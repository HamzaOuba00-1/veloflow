"""Tests for rule R6, the daily coverage report."""

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from veloflow.collector.gbfs import RawResponse
from veloflow.collector.storage import STATUS_DEGRADED, STATUS_FORMAT_ERROR, RawArchive
from veloflow.quality.coverage import coverage_report, main, report_path, write_report

DAY = date(2026, 11, 9)
MIDNIGHT = datetime(2026, 11, 9, tzinfo=UTC)


def archive_day(data_dir: Path, records: Iterable[tuple[int, str]]) -> Path:
    """Archive one response per (second of the day, status), fingerprint = second."""
    archive = RawArchive(data_dir)
    path = archive.path_for("station_status", DAY)
    for second, status in records:
        response = RawResponse(content=b"{}", fetched_at=MIDNIGHT + timedelta(seconds=second))
        path = archive.append("station_status", response, fingerprint=str(second), status=status)
    return path


def test_regular_day_with_a_degraded_version_and_a_silence(tmp_path: Path) -> None:
    # One response a minute for 20 minutes; minute 5 degraded; minutes 11-13 silent.
    minutes = [m for m in range(20) if m not in (11, 12, 13)]
    records = [(60 * m + 8, STATUS_DEGRADED if m == 5 else "ok") for m in minutes]
    path = archive_day(tmp_path, records)

    report = coverage_report(path, DAY)

    assert report.expected_versions == 20
    assert report.responses == {"degraded": 1, "ok": 16}
    assert report.source_failures == 1
    assert report.silent_missed_versions == 3
    assert [(gap.seconds, gap.missed_versions) for gap in report.gaps] == [(240.0, 3)]
    assert report.usable_share == 0.8
    assert report.source_failure_share == 0.05
    assert report.silent_missed_share == 0.15


def test_jitter_below_two_minutes_is_not_a_gap(tmp_path: Path) -> None:
    # Versions every 59-61 s plus one late by 50 s: no version was missed.
    path = archive_day(tmp_path, [(8, "ok"), (67, "ok"), (128, "ok"), (238, "ok"), (299, "ok")])

    report = coverage_report(path, DAY)

    assert report.gaps == []
    assert report.silent_missed_versions == 0


def test_format_errors_are_source_failures_and_restarts_are_duplicates(tmp_path: Path) -> None:
    path = archive_day(tmp_path, [(8, "ok"), (68, STATUS_FORMAT_ERROR), (128, "ok")])
    # A restarted collector archives the last version again.
    RawArchive(tmp_path).append(
        "station_status",
        RawResponse(content=b"{}", fetched_at=MIDNIGHT + timedelta(seconds=140)),
        fingerprint="128",
    )

    report = coverage_report(path, DAY)

    assert report.duplicates == 1
    assert report.source_failures == 1
    assert report.responses == {"format_error": 1, "ok": 2}


def test_empty_file(tmp_path: Path) -> None:
    path = RawArchive(tmp_path).path_for("station_status", DAY)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")

    report = coverage_report(path, DAY)

    assert report.expected_versions == 0
    assert report.usable_share == 0.0
    assert report.first_response is None


def test_report_is_written_as_json(tmp_path: Path) -> None:
    archive_day(tmp_path, [(8, "ok"), (68, "ok")])

    path = write_report(tmp_path, DAY)

    assert path == report_path(tmp_path, DAY)
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["day"] == "2026-11-09"
    assert content["responses"] == {"ok": 2}
    assert not list(path.parent.glob("*.tmp"))


def test_missing_day_writes_nothing(tmp_path: Path) -> None:
    assert write_report(tmp_path, DAY) is None


@pytest.mark.parametrize(
    ("argv", "expected_code"),
    [
        ([], 0),  # yesterday of 10 Nov
        (["--date", "2026-11-09"], 0),
        (["--all"], 0),
        (["--date", "2026-11-01"], 1),
    ],
    ids=["yesterday", "date", "all", "missing"],
)
def test_command_line(tmp_path: Path, argv: list[str], expected_code: int) -> None:
    archive_day(tmp_path, [(8, "ok")])

    code = main(["--data-dir", str(tmp_path), *argv], today=date(2026, 11, 10))

    assert code == expected_code
    assert report_path(tmp_path, DAY).exists() == (expected_code == 0)
