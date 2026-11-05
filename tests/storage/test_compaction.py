"""Tests for the raw -> Parquet compaction job."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from veloflow.collector.gbfs import RawResponse
from veloflow.collector.storage import STATUS_DEGRADED, STATUS_FORMAT_ERROR, RawArchive
from veloflow.storage.compaction import (
    STATUS_SCHEMA,
    compact_day,
    main,
    raw_days,
    silver_path,
    status_frame,
)

type ResponseFactory = Callable[..., RawResponse]

DAY = date(2026, 11, 5)
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def shifted(minutes: int) -> Callable[[dict[str, Any]], None]:
    """Edit making a later feed version, with station 16107 losing one dock."""

    def edit(feed: dict[str, Any]) -> None:
        feed["lastUpdatedOther"] += 60 * minutes
        first = feed["data"]["stations"][0]
        first["num_docks_available"] -= minutes
        first["num_bikes_available"] += minutes
        first["num_bikes_available_types"][0]["mechanical"] += minutes

    return edit


def null_codes(feed: dict[str, Any]) -> None:
    for station in feed["data"]["stations"]:
        station["stationCode"] = None


@pytest.fixture
def raw_day(tmp_path: Path, make_response: ResponseFactory) -> Path:
    """A raw day mixing every case the compaction must handle, in arrival order."""
    archive = RawArchive(tmp_path)
    later = make_response(edit=shifted(1), fetched_at=T0 + timedelta(seconds=60))
    earlier = make_response(fetched_at=T0 + timedelta(seconds=90))  # lagging server
    archive.append("station_status", later, fingerprint="v2")
    archive.append("station_status", earlier, fingerprint="v1")
    archive.append("station_status", later, fingerprint="v2")  # collector restart
    archive.append("station_status", make_response(edit=null_codes), fingerprint="old-degraded")
    archive.append("station_status", RawResponse(b"<html>", T0), fingerprint="unparseable")
    archive.append("station_status", earlier, fingerprint=None, status=STATUS_DEGRADED)
    archive.append("station_status", earlier, fingerprint=None, status=STATUS_FORMAT_ERROR)
    return tmp_path


def test_frame_keeps_valid_versions_once_in_time_order(raw_day: Path) -> None:
    result = status_frame(RawArchive(raw_day).path_for("station_status", DAY))

    assert result.versions == 2
    assert result.duplicates == 1
    # Skipped: old-style degraded, unparseable "ok", degraded, format_error.
    assert result.skipped == 4
    assert result.frame.height == 2 * 9
    assert result.frame["feed_updated_at"].is_sorted()
    assert dict(result.frame.schema) == STATUS_SCHEMA


def test_frame_values_match_the_feed(raw_day: Path) -> None:
    frame = status_frame(RawArchive(raw_day).path_for("station_status", DAY)).frame

    station = frame.filter(pl.col("station_id") == 213688169)
    assert station["mechanical"].to_list() == [4, 5]
    assert station["docks"].to_list() == [29, 28]
    assert station.schema["feed_updated_at"] == pl.Datetime("us", "UTC")


def test_compact_day_writes_readable_parquet(raw_day: Path) -> None:
    path = compact_day(raw_day, DAY)

    assert path == silver_path(raw_day, DAY)
    assert path is not None
    assert path.parent.name == "date=2026-11-05"
    assert not list(path.parent.glob("*.tmp"))  # atomic write left nothing behind
    assert pl.read_parquet(path).height == 18


def test_compact_day_is_idempotent(raw_day: Path) -> None:
    first = compact_day(raw_day, DAY)
    assert first is not None
    before = pl.read_parquet(first)

    second = compact_day(raw_day, DAY)

    assert second == first
    assert pl.read_parquet(first).equals(before)


def test_missing_raw_day_returns_none(tmp_path: Path) -> None:
    assert compact_day(tmp_path, DAY) is None


def test_empty_day_gives_an_empty_table_with_the_schema(tmp_path: Path) -> None:
    raw = RawArchive(tmp_path).path_for("station_status", DAY)
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"")

    frame = status_frame(raw).frame

    assert frame.height == 0
    assert dict(frame.schema) == STATUS_SCHEMA


def test_raw_days_lists_archive_files(raw_day: Path, make_response: ResponseFactory) -> None:
    next_day = make_response(fetched_at=T0 + timedelta(days=1))
    RawArchive(raw_day).append("station_status", next_day, fingerprint="x")

    assert raw_days(raw_day) == [DAY, DAY + timedelta(days=1)]


def test_cli_defaults_to_yesterday(raw_day: Path) -> None:
    code = main(["--data-dir", str(raw_day)], today=DAY + timedelta(days=1))

    assert code == 0
    assert silver_path(raw_day, DAY).exists()


def test_cli_explicit_date_and_all(raw_day: Path) -> None:
    assert main(["--data-dir", str(raw_day), "--date", "2026-11-05"]) == 0
    assert main(["--data-dir", str(raw_day), "--all"]) == 0


def test_cli_fails_when_nothing_was_compacted(tmp_path: Path) -> None:
    assert main(["--data-dir", str(tmp_path), "--date", "2026-11-05"]) == 1
