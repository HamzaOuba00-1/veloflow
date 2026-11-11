"""Tests for the shared data loaders."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from veloflow.collector.gbfs import RawResponse
from veloflow.collector.storage import STATUS_DEGRADED, RawArchive
from veloflow.datasets import (
    INFORMATION_SCHEMA,
    load_station_information,
    load_station_status,
    resample_status,
    silver_days,
)
from veloflow.storage.compaction import STATUS_SCHEMA, silver_path

MIDNIGHT = datetime(2026, 11, 9, tzinfo=UTC)


def write_day(data_dir: Path, day: date, versions: list[tuple[datetime, datetime]]) -> None:
    """Silver file of ``day`` with one row per (fetch, feed) version, station 1."""
    rows = [(feed, fetched, 1, 3, 1, 5, True, True, True, feed) for fetched, feed in versions]
    path = silver_path(data_dir, day)
    path.parent.mkdir(parents=True)
    pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row").write_parquet(path)


def test_days_are_read_in_range_and_cleaned_across_midnight(tmp_path: Path) -> None:
    before = MIDNIGHT - timedelta(seconds=30)
    write_day(tmp_path, date(2026, 11, 8), [(before + timedelta(seconds=8), before)])
    # First version of the 9th carries a feed time older than the 8th's last.
    write_day(
        tmp_path,
        date(2026, 11, 9),
        [
            (MIDNIGHT + timedelta(seconds=8), before - timedelta(seconds=5)),
            (MIDNIGHT + timedelta(seconds=68), MIDNIGHT + timedelta(seconds=60)),
        ],
    )
    write_day(tmp_path, date(2026, 11, 10), [(MIDNIGHT + timedelta(days=1), MIDNIGHT)])

    data = load_station_status(tmp_path, date(2026, 11, 8), date(2026, 11, 9))

    assert silver_days(tmp_path) == [date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)]
    assert data.days == [date(2026, 11, 8), date(2026, 11, 9)]
    assert (data.versions_kept, data.versions_stale, data.ties_replaced) == (2, 1, 0)
    assert data.frame["operational"].to_list() == [True, True]


def test_no_data(tmp_path: Path) -> None:
    data = load_station_status(tmp_path)

    assert data.days == []
    assert data.frame.height == 0
    assert "operational" in data.frame.columns


def test_station_information_keeps_latest_attributes(
    tmp_path: Path, make_response: Callable[..., RawResponse]
) -> None:
    archive = RawArchive(tmp_path)
    first = make_response("station_information.json", fetched_at=MIDNIGHT)

    def grow_and_add(feed: dict[str, Any]) -> None:
        stations = feed["data"]["stations"]
        stations[0]["capacity"] += 10
        stations.append({**stations[1], "station_id": 999, "stationCode": "99999"})

    second = make_response(
        "station_information.json", edit=grow_and_add, fetched_at=MIDNIGHT + timedelta(hours=1)
    )
    broken = RawResponse(content=b"{}", fetched_at=MIDNIGHT + timedelta(hours=2))
    archive.append("station_information", first, fingerprint="a")
    archive.append("station_information", second, fingerprint="b")
    archive.append("station_information", broken, fingerprint="c", status=STATUS_DEGRADED)
    archive.append("station_information", broken, fingerprint="d")

    info = load_station_information(tmp_path)

    assert dict(info.schema) == INFORMATION_SCHEMA
    assert info.height == 10
    original = json.loads(first.content)["data"]["stations"][0]
    grown = info.filter(pl.col("station_id") == original["station_id"]).row(0, named=True)
    added = info.filter(pl.col("station_id") == 999).row(0, named=True)
    assert grown["capacity"] == original["capacity"] + 10
    assert grown["first_seen"] == MIDNIGHT
    assert grown["last_seen"] == MIDNIGHT + timedelta(hours=1)
    assert added["first_seen"] == MIDNIGHT + timedelta(hours=1)


def test_station_information_without_archive(tmp_path: Path) -> None:
    assert load_station_information(tmp_path).height == 0


def status_rows(*versions: tuple[int, int]) -> pl.DataFrame:
    """Station 1 with ``bikes`` mechanical bikes fetched at ``second`` past midnight."""
    rows = [
        (
            MIDNIGHT + timedelta(seconds=second),
            MIDNIGHT + timedelta(seconds=second),
            1,
            bikes,
            0,
            5,
            True,
            True,
            True,
            MIDNIGHT,
        )
        for second, bikes in versions
    ]
    return pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row")


def test_resample_uses_the_last_version_before_each_grid_time() -> None:
    # Versions at 00:00:08 (3 bikes) and 00:05:20 (4 bikes); grid every 5 min.
    grid = resample_status(status_rows((8, 3), (320, 4), (610, 7)))

    assert grid["time"].to_list() == [MIDNIGHT + timedelta(minutes=m) for m in (5, 10)]
    # At 00:05 the 00:05:20 version does not exist yet: no look-ahead.
    assert grid["mechanical"].to_list() == [3, 4]


def test_resample_leaves_gaps_null_beyond_the_tolerance() -> None:
    # Nothing between 00:00:08 and 00:20:08: at 00:10, 00:15 and 00:20 the
    # last known state is more than 5 minutes old.
    grid = resample_status(status_rows((8, 3), (1208, 6)), tolerance="5m")

    assert grid["mechanical"].to_list() == [3, None, None, None]


def test_resample_empty_frame() -> None:
    grid = resample_status(pl.DataFrame(schema=STATUS_SCHEMA))

    assert grid.height == 0
    assert "time" in grid.columns
