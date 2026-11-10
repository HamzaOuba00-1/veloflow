"""Tests for the archived forecast download."""

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest

from veloflow.weather.grid import GRID
from veloflow.weather.history import (
    day_ranges,
    download,
    main,
    silver_weather_path,
)
from veloflow.weather.openmeteo import OpenMeteoClient

# The recorded response covers 2 November 2026 only.
DAY = date(2026, 11, 2)


def serving(content: bytes, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _: httpx.Response(status, content=content))


def test_day_ranges_split_long_periods() -> None:
    ranges = list(day_ranges(date(2026, 11, 1), date(2026, 12, 15), size=31))

    assert ranges == [
        (date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 12, 2), date(2026, 12, 15)),
    ]


def test_day_ranges_single_day_and_invalid_order() -> None:
    assert list(day_ranges(DAY, DAY)) == [(DAY, DAY)]
    with pytest.raises(ValueError, match="before start"):
        list(day_ranges(DAY, date(2026, 11, 1)))


def test_one_file_per_day_and_resolution(tmp_path: Path, body: Callable[..., bytes]) -> None:
    with OpenMeteoClient(user_agent="test", transport=serving(body(len(GRID)))) as client:
        days = download(client, tmp_path, DAY, DAY)

    assert days == [DAY]
    hourly = pl.read_parquet(silver_weather_path(tmp_path, "hourly", DAY))
    quarters = pl.read_parquet(silver_weather_path(tmp_path, "minutely_15", DAY))
    assert hourly.height == 24 * 9
    assert quarters.height == 96 * 9
    assert "day" not in hourly.columns
    assert hourly.schema["valid_at"] == pl.Datetime("us", "UTC")
    assert hourly.select("point", "valid_at").is_duplicated().sum() == 0
    assert not list(tmp_path.rglob("*.tmp"))


def test_running_twice_rewrites_the_same_files(tmp_path: Path, body: Callable[..., bytes]) -> None:
    with OpenMeteoClient(user_agent="test", transport=serving(body(len(GRID)))) as client:
        download(client, tmp_path, DAY, DAY)
        first = pl.read_parquet(silver_weather_path(tmp_path, "hourly", DAY))
        download(client, tmp_path, DAY, DAY)

    assert pl.read_parquet(silver_weather_path(tmp_path, "hourly", DAY)).equals(first)


def test_day_not_yet_archived_is_skipped(
    tmp_path: Path, location: dict[str, Any], body: Callable[..., bytes]
) -> None:
    for block in ("hourly", "minutely_15"):
        for name, values in location[block].items():
            if name != "time":
                location[block][name] = [None] * len(values)

    with OpenMeteoClient(user_agent="test", transport=serving(body(len(GRID)))) as client:
        days = download(client, tmp_path, DAY, DAY)

    assert days == []
    assert not silver_weather_path(tmp_path, "hourly", DAY).exists()


def test_command_line_defaults_to_yesterday(tmp_path: Path, body: Callable[..., bytes]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body(len(GRID)))

    code = main(
        ["--data-dir", str(tmp_path), "--start", "2026-11-02"],
        transport=httpx.MockTransport(handler),
        today=date(2026, 11, 3),
    )

    assert code == 0
    assert requests[0].url.params["end_date"] == "2026-11-02"


@pytest.mark.parametrize(
    ("argv", "transport"),
    [
        (["--start", "2026-11-02", "--end", "2026-11-02"], serving(b"busy", status=503)),
        (["--start", "2026-11-05", "--end", "2026-11-02"], serving(b"{}")),
    ],
    ids=["server-error", "inverted-range"],
)
def test_command_line_failures(
    tmp_path: Path, argv: list[str], transport: httpx.MockTransport
) -> None:
    assert main(["--data-dir", str(tmp_path), *argv], transport=transport) == 1
