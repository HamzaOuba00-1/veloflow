"""Archive the forecast available now: ``uv run veloflow-weather-snapshot``.

Run every hour on the VM by a systemd timer. Each snapshot records what the
model would have known at that time, which the Historical Forecast archive
does not tell exactly (it stitches the first hours of successive runs). The
two can then be compared to measure the training/serving gap (decision D23).

Snapshots go to the bronze layer, next to the GBFS responses::

    data/raw/weather_forecast/weather_forecast_2026-11-10.jsonl.gz
"""

import argparse
import hashlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from veloflow.collector.storage import STATUS_FORMAT_ERROR, STATUS_OK, RawArchive
from veloflow.weather.grid import GRID, GridPoint
from veloflow.weather.openmeteo import (
    OpenMeteoClient,
    WeatherError,
    WeatherFormatError,
    parse_weather,
)

logger = logging.getLogger(__name__)

FEED = "weather_forecast"
USER_AGENT = "VeloFlow-weather/0.1 (+https://github.com/HamzaOuba00-1/veloflow)"


def take_snapshot(
    client: OpenMeteoClient, archive: RawArchive, points: Sequence[GridPoint] = GRID
) -> Path:
    """Download the current forecast, check it parses, and archive it.

    A response that fails parsing is archived too, as evidence, then the
    error is raised.

    Raises:
        WeatherUnavailableError: the download failed.
        WeatherFormatError: the response has an unexpected structure.
    """
    response = client.fetch_forecast(points)
    fingerprint = hashlib.sha256(response.content).hexdigest()
    try:
        hourly = parse_weather(response.content, points, "hourly")
        quarters = parse_weather(response.content, points, "minutely_15")
    except WeatherFormatError:
        archive.append(FEED, response, fingerprint=fingerprint, status=STATUS_FORMAT_ERROR)
        raise
    path = archive.append(FEED, response, fingerprint=fingerprint, status=STATUS_OK)
    logger.info(
        "forecast archived: %d points, %d hourly and %d 15-minute rows, %d bytes",
        len(points),
        hourly.height,
        quarters.height,
        len(response.content),
    )
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    """Take one snapshot; exit code 1 on failure so systemd reports it."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO; ours are long and add nothing.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)
    with OpenMeteoClient(user_agent=USER_AGENT, transport=transport) as client:
        try:
            take_snapshot(client, RawArchive(args.data_dir))
        except WeatherError as error:
            logger.error("snapshot failed: %s", error)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
