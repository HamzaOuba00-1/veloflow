"""Turn one day of raw ``station_status`` archive into a Parquet table (silver layer).

Raw (bronze, kept on the VM)::

    data/raw/station_status/station_status_2026-11-05.jsonl.gz

Compacted (silver, shipped to the laptop)::

    data/silver/station_status/date=2026-11-05/station_status.parquet

One row per station per feed version. Storing full snapshots rather than only
the stations that changed was decided on measurements: with Parquet + zstd both
weigh about the same (~2.5 MB/day), and full snapshots need no reconstruction
when read (see docs/data/storage.md).

The job is idempotent: running it again on the same day rewrites the same file.
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from veloflow.collector.gbfs import FeedFormatError, parse_station_status
from veloflow.collector.service import MAX_REJECTED_SHARE, STATUS_FEED
from veloflow.collector.storage import STATUS_OK, RawArchive, iter_records

logger = logging.getLogger(__name__)

# Compact integer types: a station never holds more than 32,767 bikes.
STATUS_SCHEMA: dict[str, pl.DataType] = {
    "feed_updated_at": pl.Datetime("us", "UTC"),
    "fetched_at": pl.Datetime("us", "UTC"),
    "station_id": pl.Int64(),
    "mechanical": pl.Int16(),
    "ebike": pl.Int16(),
    "docks": pl.Int16(),
    "is_installed": pl.Boolean(),
    "is_renting": pl.Boolean(),
    "is_returning": pl.Boolean(),
    "last_reported": pl.Datetime("us", "UTC"),
}


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Outcome of compacting one day."""

    frame: pl.DataFrame
    versions: int
    duplicates: int
    skipped: int


def silver_path(data_dir: Path, day: date) -> Path:
    """Parquet file of ``day``, in a Hive-style ``date=`` folder (read as a column by Polars)."""
    return data_dir / "silver" / STATUS_FEED / f"date={day.isoformat()}" / f"{STATUS_FEED}.parquet"


def status_frame(raw_path: Path) -> CompactionResult:
    """Read one raw daily file and return its valid versions as a table.

    Skipped: records not archived as ``ok``, records that no longer parse, and
    degraded versions archived before the collector knew how to flag them.
    Duplicates (same fingerprint, e.g. after a collector restart) are kept once.
    """
    frames: list[tuple[datetime, pl.DataFrame]] = []
    seen: set[str] = set()
    duplicates = skipped = 0

    for record in iter_records(raw_path):
        if record.get("status") != STATUS_OK:
            skipped += 1
            continue
        if record["fingerprint"] in seen:
            duplicates += 1
            continue
        try:
            snapshot = parse_station_status(
                record["content"].encode(), datetime.fromisoformat(record["fetched_at"])
            )
        except FeedFormatError:
            skipped += 1
            continue
        if snapshot.rejected_share > MAX_REJECTED_SHARE:
            skipped += 1
            continue
        seen.add(record["fingerprint"])
        # One small frame per version keeps peak memory low on the 1 GB VM:
        # Python objects for 1,520 stations at a time, compact columns after.
        rows = [
            (
                snapshot.feed_updated_at,
                snapshot.fetched_at,
                s.station_id,
                s.mechanical,
                s.ebike,
                s.docks,
                s.is_installed,
                s.is_renting,
                s.is_returning,
                s.last_reported,
            )
            for s in snapshot.stations
        ]
        version = pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row").sort("station_id")
        frames.append((snapshot.feed_updated_at, version))

    # Arrival order is not time order (several servers): order the versions by
    # feed time, then stack them. Sorting 1,440 small frames instead of one
    # 2.2-million-row table avoids a full copy and halves peak memory.
    frames.sort(key=lambda item: item[0])
    frame = (
        pl.concat([version for _, version in frames], how="vertical", rechunk=False)
        if frames
        else pl.DataFrame(schema=STATUS_SCHEMA)
    )
    return CompactionResult(frame, versions=len(frames), duplicates=duplicates, skipped=skipped)


def compact_day(data_dir: Path, day: date) -> Path | None:
    """Compact ``day`` into its silver Parquet file; return its path, or None if no raw data."""
    raw_path = RawArchive(data_dir).path_for(STATUS_FEED, day)
    if not raw_path.exists():
        logger.warning("no raw station_status file for %s", day)
        return None

    result = status_frame(raw_path)
    target = silver_path(data_dir, day)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temporary name, then rename: a reader (or the sync job) never
    # sees a half-written file. A rename is atomic on the same file system.
    temporary = target.with_suffix(".parquet.tmp")
    result.frame.write_parquet(temporary, compression="zstd", statistics=True)
    temporary.replace(target)

    logger.info(
        "%s: %d versions, %d rows, %d duplicates, %d skipped, %.1f KB",
        day,
        result.versions,
        result.frame.height,
        result.duplicates,
        result.skipped,
        target.stat().st_size / 1024,
    )
    return target


def raw_days(data_dir: Path) -> list[date]:
    """Every UTC day with a raw station_status file."""
    folder = data_dir / "raw" / STATUS_FEED
    prefix = f"{STATUS_FEED}_"
    return sorted(
        date.fromisoformat(path.name.removeprefix(prefix).removesuffix(".jsonl.gz"))
        for path in folder.glob(f"{prefix}*.jsonl.gz")
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="Compact raw station_status into Parquet.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--date", type=date.fromisoformat, help="UTC day, YYYY-MM-DD")
    which.add_argument("--all", action="store_true", help="every day found in the raw archive")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    """Compact yesterday (UTC) by default, a given day, or every raw day."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    if args.all:
        days = raw_days(args.data_dir)
    elif args.date is not None:
        days = [args.date]
    else:
        # Yesterday is complete; today's file is still being written.
        days = [(today or datetime.now(UTC).date()) - timedelta(days=1)]

    written = [compact_day(args.data_dir, day) for day in days]
    return 0 if any(path is not None for path in written) else 1


if __name__ == "__main__":
    sys.exit(main())
