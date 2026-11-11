"""Load the collected data, cleaned, for analysis, backtests and training.

Every consumer (exploration scripts, backtests, training) goes through these
functions, so that they all see the same data with the same quality rules
(docs/data/quality.md):

* ``load_station_status``: silver ``station_status`` over a range of UTC days,
  with rule R1 (versions in feed-time order) and rule R3 (``operational``);
* ``load_station_information``: the latest known attributes of every station,
  from the raw ``station_information`` archive.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import polars as pl

from veloflow.collector.gbfs import FeedFormatError, parse_station_information
from veloflow.collector.storage import STATUS_OK, iter_records
from veloflow.quality.versions import select_versions, with_operational
from veloflow.storage.compaction import STATUS_SCHEMA

logger = logging.getLogger(__name__)

INFORMATION_FEED = "station_information"

INFORMATION_SCHEMA: dict[str, pl.DataType] = {
    "station_id": pl.Int64(),
    "station_code": pl.String(),
    "name": pl.String(),
    "lat": pl.Float64(),
    "lon": pl.Float64(),
    "capacity": pl.Int16(),
    "first_seen": pl.Datetime("us", "UTC"),
    "last_seen": pl.Datetime("us", "UTC"),
}


@dataclass(frozen=True, slots=True)
class StatusData:
    """Cleaned ``station_status`` rows and what the cleaning removed."""

    frame: pl.DataFrame
    days: list[date]
    versions_kept: int
    versions_stale: int
    ties_replaced: int


def silver_days(data_dir: Path) -> list[date]:
    """Every UTC day with a silver ``station_status`` file."""
    folder = data_dir / "silver" / "station_status"
    return sorted(
        date.fromisoformat(path.parent.name.removeprefix("date="))
        for path in folder.glob("date=*/station_status.parquet")
    )


def load_station_status(
    data_dir: Path, start: date | None = None, end: date | None = None
) -> StatusData:
    """Read silver ``station_status`` for ``[start, end]`` (all days by default).

    Rule R1 runs on the whole range at once, so a stale version at the start
    of a day is compared with the end of the previous day.

    Returns:
        Rows of the kept versions, ordered by fetch time then station, with
        the extra column ``operational``.
    """
    days = [
        day
        for day in silver_days(data_dir)
        if (start is None or day >= start) and (end is None or day <= end)
    ]
    frames = [
        pl.read_parquet(
            data_dir / "silver" / "station_status" / f"date={day}" / "station_status.parquet"
        )
        for day in days
    ]
    raw = pl.concat(frames) if frames else pl.DataFrame(schema=STATUS_SCHEMA)
    selection = select_versions(raw)
    return StatusData(
        frame=with_operational(selection.frame),
        days=days,
        versions_kept=selection.kept,
        versions_stale=selection.stale,
        ties_replaced=selection.replaced_ties,
    )


def load_station_information(data_dir: Path) -> pl.DataFrame:
    """Latest attributes of every station seen in the raw reference archive.

    Versions not archived as ``ok`` or that no longer parse are ignored.
    ``first_seen`` and ``last_seen`` are the fetch times of the first and last
    versions listing the station: a station absent from the latest version
    has left the network.
    """
    latest: dict[int, tuple[object, ...]] = {}
    first_seen: dict[int, datetime] = {}
    paths = sorted((data_dir / "raw" / INFORMATION_FEED).glob(f"{INFORMATION_FEED}_*.jsonl.gz"))
    for path in paths:
        for record in iter_records(path):
            if record.get("status") != STATUS_OK:
                continue
            fetched_at = datetime.fromisoformat(record["fetched_at"])
            try:
                snapshot = parse_station_information(record["content"].encode(), fetched_at)
            except FeedFormatError:
                logger.warning("%s: unreadable version at %s, ignored", path.name, fetched_at)
                continue
            for station in snapshot.stations:
                first_seen.setdefault(station.station_id, fetched_at)
                latest[station.station_id] = (
                    station.station_id,
                    station.station_code,
                    station.name,
                    station.lat,
                    station.lon,
                    station.capacity,
                    first_seen[station.station_id],
                    fetched_at,
                )
    return pl.DataFrame(list(latest.values()), schema=INFORMATION_SCHEMA, orient="row").sort(
        "station_id"
    )
