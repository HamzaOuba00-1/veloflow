"""Download archived forecasts into the silver layer: ``uv run veloflow-weather-history``.

Training uses forecasts, never observations (decision D23). Open-Meteo's
Historical Forecast API serves the Météo-France forecasts archived since 2016;
this command stores them, for the nine grid points, one Parquet file per UTC day
and per resolution::

    data/silver/weather_hourly/date=2026-11-09/weather_hourly.parquet
    data/silver/weather_15min/date=2026-11-09/weather_15min.parquet

Rows keep the provider's time convention: ``valid_at`` is the end of the
interval for accumulated variables (``precipitation`` at 10:00 is the rain of
09:00-10:00). A row is filed under the UTC date of its ``valid_at``.

It runs on the laptop: the VM only archives the live forecast every hour.
Running it again on the same days rewrites the same files.
"""

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl

from veloflow.weather.grid import GRID, GridPoint
from veloflow.weather.openmeteo import (
    VARIABLES,
    OpenMeteoClient,
    Resolution,
    WeatherError,
    parse_weather,
)
from veloflow.weather.snapshot import USER_AGENT

logger = logging.getLogger(__name__)

DATASETS: dict[Resolution, str] = {"hourly": "weather_hourly", "minutely_15": "weather_15min"}

# One request covers at most this many days: a month of 15-minute data for nine
# points stays well under a megabyte, and a failure costs little to retry.
MAX_DAYS_PER_REQUEST = 31


def day_ranges(
    start: date, end: date, size: int = MAX_DAYS_PER_REQUEST
) -> Iterator[tuple[date, date]]:
    """Split ``[start, end]`` into consecutive ranges of at most ``size`` days."""
    if end < start:
        raise ValueError("end must not be before start")
    first = start
    while first <= end:
        last = min(first + timedelta(days=size - 1), end)
        yield first, last
        first = last + timedelta(days=1)


def silver_weather_path(data_dir: Path, resolution: Resolution, day: date) -> Path:
    """Parquet file of one resolution and one UTC day."""
    name = DATASETS[resolution]
    return data_dir / "silver" / name / f"date={day.isoformat()}" / f"{name}.parquet"


def write_days(data_dir: Path, frame: pl.DataFrame, resolution: Resolution) -> list[date]:
    """Write one Parquet file per UTC day of ``frame``; return the days written.

    A day whose forecast values are all missing (not yet in the archive) is
    skipped, so that a later run fills it instead of keeping an empty file.
    """
    variables = list(VARIABLES[resolution])
    written: list[date] = []
    for rows in frame.with_columns(day=pl.col("valid_at").dt.date()).partition_by(
        "day", maintain_order=True
    ):
        day: date = rows["day"][0]
        values = rows.select(variables)
        if values.null_count().sum_horizontal().item() == values.height * values.width:
            logger.warning("%s %s: no forecast in the archive yet, skipped", day, resolution)
            continue
        path = silver_weather_path(data_dir, resolution, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".parquet.tmp")
        rows.drop("day").sort("point", "valid_at").write_parquet(temporary, compression="zstd")
        temporary.replace(path)
        written.append(day)
    return written


def download(
    client: OpenMeteoClient,
    data_dir: Path,
    start: date,
    end: date,
    points: Sequence[GridPoint] = GRID,
) -> list[date]:
    """Download and store ``[start, end]``; return the days with hourly data written.

    Raises:
        WeatherUnavailableError: a request failed.
        WeatherFormatError: a response has an unexpected structure.
    """
    days: list[date] = []
    for first, last in day_ranges(start, end):
        response = client.fetch_historical_forecast(points, first, last)
        for resolution in DATASETS:
            frame = parse_weather(response.content, points, resolution)
            written = write_days(data_dir, frame, resolution)
            if resolution == "hourly":
                days.extend(written)
        logger.info("%s to %s: %d days stored", first, last, (last - first).days + 1)
    return days


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--end", type=date.fromisoformat, help="YYYY-MM-DD, included (default: yesterday, UTC)"
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    today: date | None = None,
) -> int:
    """Download the requested days; exit code 1 on failure or if nothing was stored."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)
    end = args.end or (today or datetime.now(UTC).date()) - timedelta(days=1)
    with OpenMeteoClient(user_agent=USER_AGENT, transport=transport) as client:
        try:
            days = download(client, args.data_dir, args.start, end)
        except (WeatherError, ValueError) as error:
            logger.error("download failed: %s", error)
            return 1
    logger.info("%d days of archived forecasts in %s", len(days), args.data_dir.resolve())
    return 0 if days else 1


if __name__ == "__main__":
    sys.exit(main())
