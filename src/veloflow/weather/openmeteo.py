"""Client and parser for the Open-Meteo forecast APIs.

Like ``collector/gbfs.py``, this is the only module that knows the provider's
wire format, split in two layers:

* ``OpenMeteoClient`` performs HTTP calls and returns raw bytes (I/O);
* ``parse_weather`` turns those bytes into a tidy Polars frame (pure function).

Two endpoints serve the same format:

* the Forecast API gives the forecast available right now (inference, and the
  hourly snapshots archived on the VM);
* the Historical Forecast API gives forecasts archived since 2016 (training).

Time convention (measured, see docs/data/weather.md): times are in UTC and
label the **end** of an interval for accumulated variables. ``precipitation``
at 10:00 is the rain of 09:00-10:00 (hourly) or 09:45-10:00 (15 minutes).
Instantaneous variables (temperature, wind) are the value at that time.

Data: Open-Meteo, CC BY 4.0. Free API for non-commercial use, under 10,000
calls a day (each location counts).
"""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, Literal, Self

import httpx
import polars as pl
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from veloflow.collector.gbfs import RawResponse
from veloflow.weather.grid import GridPoint

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Météo-France AROME/ARPEGE: native 15-minute steps over France. The default
# ``best_match`` model interpolated 86 % of its 15-minute values from hourly
# ones over Paris (decision D23).
MODEL = "meteofrance_seamless"

HOURLY_VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "weather_code",
    "wind_speed_10m",
    "wind_gusts_10m",
)
MINUTELY_15_VARIABLES: tuple[str, ...] = ("temperature_2m", "precipitation", "wind_speed_10m")

type Resolution = Literal["hourly", "minutely_15"]

VARIABLES: dict[Resolution, tuple[str, ...]] = {
    "hourly": HOURLY_VARIABLES,
    "minutely_15": MINUTELY_15_VARIABLES,
}

# WMO weather codes are small integers; every other variable is a measurement.
_INTEGER_VARIABLES = frozenset({"weather_code"})


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class WeatherError(Exception):
    """Base class for every weather provider problem."""


class WeatherUnavailableError(WeatherError):
    """The forecast could not be downloaded (network, timeout, HTTP error)."""


class WeatherFormatError(WeatherError):
    """The response was downloaded but does not have the expected structure."""


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


class _Location(BaseModel):
    """One location of a response: a lone object, or one item of a list."""

    model_config = ConfigDict(extra="ignore")

    latitude: float
    longitude: float
    utc_offset_seconds: int
    hourly: dict[str, list[Any]] | None = None
    minutely_15: dict[str, list[Any]] | None = None


_LOCATIONS = TypeAdapter(list[_Location])


def parse_weather(
    content: bytes, points: Sequence[GridPoint], resolution: Resolution
) -> pl.DataFrame:
    """Turn a response into one row per grid point and time.

    Args:
        content: body returned for a request on ``points``, in that order.
        points: grid points of the request; the API answers in request order.
        resolution: block to read, ``"hourly"`` or ``"minutely_15"``.

    Returns:
        Columns ``point``, ``valid_at`` (UTC) and one column per variable of
        ``VARIABLES[resolution]``. Missing values stay null.

    Raises:
        WeatherFormatError: the body is not the expected structure.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise WeatherFormatError(f"body is not JSON: {error}") from error
    if isinstance(data, dict) and data.get("error"):
        raise WeatherFormatError(f"API error: {data.get('reason')}")
    # One location comes back as an object, several as a list.
    try:
        locations = _LOCATIONS.validate_python(data if isinstance(data, list) else [data])
    except ValidationError as error:
        raise WeatherFormatError(f"unexpected structure: {error}") from error
    if len(locations) != len(points):
        raise WeatherFormatError(f"{len(locations)} locations for {len(points)} points requested")

    variables = VARIABLES[resolution]
    frames = [
        _location_frame(location, point, resolution, variables)
        for location, point in zip(locations, points, strict=True)
    ]
    return pl.concat(frames)


def _location_frame(
    location: _Location, point: GridPoint, resolution: Resolution, variables: Sequence[str]
) -> pl.DataFrame:
    if location.utc_offset_seconds != 0:
        raise WeatherFormatError(
            f"{point.code}: times are not UTC (offset {location.utc_offset_seconds} s)"
        )
    block = location.hourly if resolution == "hourly" else location.minutely_15
    if block is None:
        raise WeatherFormatError(f"{point.code}: no {resolution} block")
    missing = [name for name in ("time", *variables) if name not in block]
    if missing:
        raise WeatherFormatError(f"{point.code}: {resolution} lacks {missing}")
    lengths = {len(block[name]) for name in ("time", *variables)}
    if len(lengths) != 1:
        raise WeatherFormatError(f"{point.code}: {resolution} columns differ in length")

    try:
        frame = pl.DataFrame(
            {
                "point": [point.code] * len(block["time"]),
                "valid_at": pl.Series(block["time"], dtype=pl.String).str.to_datetime(
                    "%Y-%m-%dT%H:%M", time_unit="us", time_zone="UTC"
                ),
                **{
                    name: pl.Series(
                        name,
                        block[name],
                        dtype=pl.Int16 if name in _INTEGER_VARIABLES else pl.Float64,
                        strict=True,
                    )
                    for name in variables
                },
            }
        )
    except (pl.exceptions.PolarsError, TypeError) as error:
        raise WeatherFormatError(
            f"{point.code}: unreadable {resolution} values: {error}"
        ) from error
    if frame["valid_at"].null_count() or not frame["valid_at"].is_sorted():
        raise WeatherFormatError(f"{point.code}: {resolution} times are invalid or unsorted")
    return frame


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class OpenMeteoClient:
    """Synchronous HTTP client for the Open-Meteo forecast APIs.

    One request covers every grid point (comma-separated coordinates). Use it
    as a context manager so the connection pool is always closed.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        forecast_url: str = FORECAST_URL,
        historical_forecast_url: str = HISTORICAL_FORECAST_URL,
    ) -> None:
        """Create the client.

        Args:
            user_agent: identifies VéloFlow to the provider.
            timeout_s: per-request timeout.
            transport: injected in tests to simulate the server.
            forecast_url: Forecast API endpoint.
            historical_forecast_url: Historical Forecast API endpoint.
        """
        self._forecast_url = forecast_url
        self._historical_forecast_url = historical_forecast_url
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection pool."""
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def fetch_forecast(self, points: Sequence[GridPoint], *, hours: int = 24) -> RawResponse:
        """Download the forecast available now for the next ``hours`` hours.

        24 hours is far beyond our 60-minute horizon on purpose: if later
        snapshots fail, the last one still covers the following hours.
        """
        if hours < 1:
            raise ValueError("hours must be at least 1")
        params = self._params(points)
        params |= {"forecast_hours": hours, "forecast_minutely_15": hours * 4}
        return self._get(self._forecast_url, params)

    def fetch_historical_forecast(
        self, points: Sequence[GridPoint], start: date, end: date
    ) -> RawResponse:
        """Download archived forecasts for the UTC days ``start`` to ``end`` included."""
        if end < start:
            raise ValueError("end must not be before start")
        params = self._params(points)
        params |= {"start_date": start.isoformat(), "end_date": end.isoformat()}
        return self._get(self._historical_forecast_url, params)

    @staticmethod
    def _params(points: Sequence[GridPoint]) -> dict[str, str | int]:
        if not points:
            raise ValueError("at least one point is required")
        return {
            "latitude": ",".join(f"{point.lat}" for point in points),
            "longitude": ",".join(f"{point.lon}" for point in points),
            "hourly": ",".join(HOURLY_VARIABLES),
            "minutely_15": ",".join(MINUTELY_15_VARIABLES),
            "models": MODEL,
            "timezone": "GMT",
        }

    def _get(self, url: str, params: dict[str, str | int]) -> RawResponse:
        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as error:
            raise WeatherUnavailableError(f"GET {url} failed: {error!r}") from error
        if response.is_error:
            # The API explains rejected requests in a JSON "reason" field.
            try:
                reason = response.json().get("reason", "")
            except (ValueError, AttributeError):
                reason = ""
            raise WeatherUnavailableError(
                f"GET {url} returned HTTP {response.status_code} {reason}".rstrip()
            )
        return RawResponse(content=response.content, fetched_at=datetime.now(UTC))
