"""Rule R6: daily coverage report of the ``station_status`` collection.

The specification asks for "less than 1 % of missed minutes, gaps listed and
explained". A minute without a usable version has two very different causes:

* the **source** published something unusable: a degraded version (too many
  invalid stations) or a body that does not parse. We received it and kept it
  as evidence; nothing on our side can fix it;
* **nothing was received**: the collector was down, or the feed stayed silent.
  These show up as gaps between consecutive archived responses.

Mixing the two would blame the collector for the operator's failures (on
9 November 2026: 20 degraded versions, 1.4 %, against about 2 silent minutes).

Usage: ``uv run veloflow-coverage`` (yesterday, UTC), ``--date 2026-11-09`` or
``--all``. Reports are written as JSON next to the silver layer::

    data/quality/coverage/coverage_2026-11-09.json
"""

import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from veloflow.collector.service import STATUS_FEED
from veloflow.collector.storage import STATUS_OK, RawArchive, iter_records
from veloflow.storage.compaction import raw_days

logger = logging.getLogger(__name__)

# The feed publishes a version every 59-61 s: a longer silence means at least
# one version was missed.
GAP_THRESHOLD_S = 120.0
VERSION_PERIOD_S = 60.0


@dataclass(frozen=True, slots=True)
class Gap:
    """A silence between two consecutive archived responses."""

    start: str
    end: str
    seconds: float
    missed_versions: int


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Coverage of one UTC day of raw ``station_status``."""

    day: str
    first_response: str | None
    last_response: str | None
    expected_versions: int
    responses: dict[str, int]
    duplicates: int
    source_failures: int
    silent_missed_versions: int
    usable_share: float
    source_failure_share: float
    silent_missed_share: float
    gaps: list[Gap] = field(default_factory=list)


def coverage_report(raw_path: Path, day: date) -> CoverageReport:
    """Measure the coverage of one raw daily file.

    The expected number of versions runs from the first response of the day
    (the collection may start mid-day) to the last one, one per minute.
    """
    statuses: Counter[str] = Counter()
    seen: set[str] = set()
    duplicates = 0
    times: list[datetime] = []
    for record in iter_records(raw_path):
        fingerprint = record.get("fingerprint")
        if fingerprint is not None and fingerprint in seen:
            # Re-archived after a collector restart: not a new version.
            duplicates += 1
            continue
        if fingerprint is not None:
            seen.add(fingerprint)
        statuses[str(record.get("status"))] += 1
        times.append(datetime.fromisoformat(record["fetched_at"]))

    times.sort()
    gaps = [
        Gap(
            start=earlier.isoformat(),
            end=later.isoformat(),
            seconds=(later - earlier).total_seconds(),
            missed_versions=round((later - earlier).total_seconds() / VERSION_PERIOD_S) - 1,
        )
        for earlier, later in pairwise(times)
        if (later - earlier).total_seconds() > GAP_THRESHOLD_S
    ]

    received = sum(statuses.values())
    expected = (
        max(received, round((times[-1] - times[0]).total_seconds() / VERSION_PERIOD_S) + 1)
        if times
        else 0
    )
    usable = statuses[STATUS_OK]
    source_failures = received - usable
    silent = sum(gap.missed_versions for gap in gaps)
    return CoverageReport(
        day=day.isoformat(),
        first_response=times[0].isoformat() if times else None,
        last_response=times[-1].isoformat() if times else None,
        expected_versions=expected,
        responses=dict(sorted(statuses.items())),
        duplicates=duplicates,
        source_failures=source_failures,
        silent_missed_versions=silent,
        usable_share=_share(usable, expected),
        source_failure_share=_share(source_failures, expected),
        silent_missed_share=_share(silent, expected),
        gaps=gaps,
    )


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def report_path(data_dir: Path, day: date) -> Path:
    """JSON report of ``day``."""
    return data_dir / "quality" / "coverage" / f"coverage_{day.isoformat()}.json"


def write_report(data_dir: Path, day: date) -> Path | None:
    """Write the report of ``day``; return its path, or None without raw data."""
    raw_path = RawArchive(data_dir).path_for(STATUS_FEED, day)
    if not raw_path.exists():
        logger.warning("%s: no raw file, nothing to report", day)
        return None
    report = coverage_report(raw_path, day)
    path = report_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    logger.info(
        "%s: %d expected, %.2f %% usable, %.2f %% source failures, %.2f %% silent (%d gaps)",
        day,
        report.expected_versions,
        100 * report.usable_share,
        100 * report.source_failure_share,
        100 * report.silent_missed_share,
        len(report.gaps),
    )
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--date", type=date.fromisoformat, help="UTC day, YYYY-MM-DD")
    which.add_argument("--all", action="store_true", help="every day with raw data")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    """Report yesterday (UTC) by default, a given day, or every raw day."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    if args.all:
        days = raw_days(args.data_dir)
    elif args.date is not None:
        days = [args.date]
    else:
        days = [(today or datetime.now(UTC).date()) - timedelta(days=1)]
    written = [write_report(args.data_dir, day) for day in days]
    return 0 if any(path is not None for path in written) else 1


if __name__ == "__main__":
    sys.exit(main())
