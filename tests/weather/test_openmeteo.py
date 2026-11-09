"""Tests for the Open-Meteo client and parser."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import polars as pl
import pytest

from veloflow.weather.grid import GRID, GridPoint
from veloflow.weather.openmeteo import (
    HOURLY_VARIABLES,
    MINUTELY_15_VARIABLES,
    MODEL,
    OpenMeteoClient,
    WeatherFormatError,
    WeatherUnavailableError,
    parse_weather,
)

CENTRE = [GridPoint("C", 48.849, 2.352)]


def encode(payload: Any) -> bytes:
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_hourly_block_of_one_location(body: Callable[..., bytes]) -> None:
    frame = parse_weather(body(), CENTRE, "hourly")

    assert frame.columns == ["point", "valid_at", *HOURLY_VARIABLES]
    assert frame.height == 24
    assert frame.schema["valid_at"] == pl.Datetime("us", "UTC")
    assert frame.schema["weather_code"] == pl.Int16
    assert frame.schema["temperature_2m"] == pl.Float64
    assert frame["valid_at"][0] == datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert frame["point"].unique().to_list() == ["C"]
    assert frame.null_count().sum_horizontal().item() == 0


def test_minutely_15_block_of_one_location(body: Callable[..., bytes]) -> None:
    frame = parse_weather(body(), CENTRE, "minutely_15")

    assert frame.columns == ["point", "valid_at", *MINUTELY_15_VARIABLES]
    assert frame.height == 96
    assert frame["valid_at"][1] == datetime(2026, 11, 2, 0, 15, tzinfo=UTC)


def test_hourly_precipitation_is_the_rain_of_the_hour_before(body: Callable[..., bytes]) -> None:
    """Pins the time convention measured on the real response.

    The value at 17:00 is the sum of the quarters ending at 16:15, 16:30,
    16:45 and 17:00: the label is the end of the interval. Reading it as the
    start would shift every rain feature by one hour.
    """
    hourly = parse_weather(body(), CENTRE, "hourly")
    quarters = parse_weather(body(), CENTRE, "minutely_15")
    summed = (
        quarters.with_columns(hour=pl.col("valid_at").dt.offset_by("45m").dt.truncate("1h"))
        .group_by("hour")
        .agg(pl.col("precipitation").sum().round(2), pl.len().alias("quarters"))
        .filter(pl.col("quarters") == 4)
    )
    joined = hourly.join(summed, left_on="valid_at", right_on="hour")

    assert joined.height == 23  # 00:00 needs quarters of the previous day
    assert (joined["precipitation"] - joined["precipitation_right"]).abs().max() == 0
    assert joined.filter(pl.col("valid_at").dt.hour() == 17)["precipitation"].item() == 0.5


def test_several_locations_follow_the_request_order(body: Callable[..., bytes]) -> None:
    frame = parse_weather(body(len(GRID)), GRID, "hourly")

    assert frame.height == 24 * 9
    assert frame["point"].unique(maintain_order=True).to_list() == [p.code for p in GRID]


def test_missing_values_stay_null(location: dict[str, Any]) -> None:
    location["hourly"]["temperature_2m"][5] = None

    frame = parse_weather(encode(location), CENTRE, "hourly")

    assert frame["temperature_2m"].null_count() == 1


def test_api_error_reason_is_reported() -> None:
    with pytest.raises(WeatherFormatError, match="Parameter 'models' is invalid"):
        parse_weather(
            encode({"error": True, "reason": "Parameter 'models' is invalid"}), CENTRE, "hourly"
        )


def test_body_must_be_json() -> None:
    with pytest.raises(WeatherFormatError, match="not JSON"):
        parse_weather(b"<html>maintenance</html>", CENTRE, "hourly")


def test_location_count_must_match_the_request(body: Callable[..., bytes]) -> None:
    with pytest.raises(WeatherFormatError, match="2 locations for 9 points"):
        parse_weather(body(2), GRID, "hourly")


def test_structure_errors_are_format_errors() -> None:
    with pytest.raises(WeatherFormatError, match="unexpected structure"):
        parse_weather(encode([{"latitude": "north"}]), CENTRE, "hourly")


Edit = Callable[[dict[str, Any]], None]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda loc: loc.update(utc_offset_seconds=3600), "not UTC"),
        (lambda loc: loc.pop("hourly"), "no hourly block"),
        (lambda loc: loc["hourly"].pop("rain"), "lacks"),
        (lambda loc: loc["hourly"]["rain"].pop(), "differ in length"),
        (lambda loc: loc["hourly"]["time"].__setitem__(3, "yesterday"), "unreadable"),
        (lambda loc: loc["hourly"]["temperature_2m"].__setitem__(3, "warm"), "unreadable"),
        (lambda loc: loc["hourly"]["time"].reverse(), "unsorted"),
    ],
    ids=["offset", "block", "variable", "length", "time", "value", "order"],
)
def test_malformed_locations_are_refused(
    location: dict[str, Any], edit: Edit, message: str
) -> None:
    edit(location)

    with pytest.raises(WeatherFormatError, match=message):
        parse_weather(encode(location), CENTRE, "hourly")


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


def recording_client(
    requests: list[httpx.Request], respond: Callable[[httpx.Request], httpx.Response]
) -> OpenMeteoClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    return OpenMeteoClient(user_agent="test-agent", transport=httpx.MockTransport(handler))


def test_forecast_request(body: Callable[..., bytes]) -> None:
    requests: list[httpx.Request] = []
    with recording_client(requests, lambda _: httpx.Response(200, content=body(9))) as client:
        response = client.fetch_forecast(GRID)

    params = requests[0].url.params
    assert requests[0].url.host == "api.open-meteo.com"
    assert requests[0].headers["User-Agent"] == "test-agent"
    assert params["models"] == MODEL
    assert params["timezone"] == "GMT"
    assert params["latitude"].split(",")[0] == "48.778"
    assert len(params["longitude"].split(",")) == 9
    assert params["hourly"] == ",".join(HOURLY_VARIABLES)
    assert params["minutely_15"] == ",".join(MINUTELY_15_VARIABLES)
    assert params["forecast_hours"] == "24"
    assert params["forecast_minutely_15"] == "96"
    assert response.content == body(9)
    assert response.fetched_at.tzinfo is UTC


def test_historical_forecast_request(body: Callable[..., bytes]) -> None:
    requests: list[httpx.Request] = []
    with recording_client(requests, lambda _: httpx.Response(200, content=body())) as client:
        client.fetch_historical_forecast(CENTRE, date(2026, 11, 1), date(2026, 11, 7))

    params = requests[0].url.params
    assert requests[0].url.host == "historical-forecast-api.open-meteo.com"
    assert params["start_date"] == "2026-11-01"
    assert params["end_date"] == "2026-11-07"
    assert "forecast_hours" not in params


def test_rejected_request_reports_the_reason() -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})

    with (
        recording_client([], respond) as client,
        pytest.raises(WeatherUnavailableError, match="HTTP 400 Latitude must be in range"),
    ):
        client.fetch_forecast(CENTRE)


def test_server_error_without_json_body() -> None:
    with (
        recording_client([], lambda _: httpx.Response(503, text="busy")) as client,
        pytest.raises(WeatherUnavailableError, match=r"HTTP 503$"),
    ):
        client.fetch_forecast(CENTRE)


def test_network_failure_is_unavailable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with (
        recording_client([], respond) as client,
        pytest.raises(WeatherUnavailableError, match="failed"),
    ):
        client.fetch_forecast(CENTRE)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda c: c.fetch_forecast(CENTRE, hours=0), "hours"),
        (lambda c: c.fetch_forecast([]), "at least one point"),
        (
            lambda c: c.fetch_historical_forecast(CENTRE, date(2026, 11, 7), date(2026, 11, 1)),
            "before start",
        ),
    ],
    ids=["hours", "points", "dates"],
)
def test_invalid_arguments_fail_before_any_request(
    call: Callable[[OpenMeteoClient], object], message: str
) -> None:
    requests: list[httpx.Request] = []
    with (
        recording_client(requests, lambda _: httpx.Response(200)) as client,
        pytest.raises(ValueError, match=message),
    ):
        call(client)
    assert requests == []
