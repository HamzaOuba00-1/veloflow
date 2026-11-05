"""Tests for the GBFS client and parsers, on real recorded Vélib' responses."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from veloflow.collector.gbfs import (
    FeedFormatError,
    FeedUnavailableError,
    GbfsClient,
    parse_feed_urls,
    parse_station_information,
    parse_station_status,
)
from veloflow.collector.models import StationStatus

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gbfs"
ENTRY_POINT = "https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/gbfs.json"
FETCHED_AT = datetime(2026, 11, 4, 15, 48, 55, tzinfo=UTC)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(fixture_bytes(name))
    return data


def by_code(stations: tuple[StationStatus, ...], code: str) -> StationStatus:
    return next(s for s in stations if s.station_code == code)


# --------------------------------------------------------------------------
# station_status
# --------------------------------------------------------------------------


def test_status_fixture_parses_every_station() -> None:
    snapshot = parse_station_status(fixture_bytes("station_status.json"), FETCHED_AT)

    assert len(snapshot.stations) == 9
    assert snapshot.rejected == 0
    assert snapshot.feed_updated_at == datetime(2026, 11, 4, 15, 48, 48, tzinfo=UTC)
    assert snapshot.fetched_at == FETCHED_AT


def test_status_maps_vendor_fields_to_our_model() -> None:
    snapshot = parse_station_status(fixture_bytes("station_status.json"), FETCHED_AT)

    station = by_code(snapshot.stations, "16107")
    assert station.station_id == 213688169
    assert (station.mechanical, station.ebike, station.bikes, station.docks) == (4, 2, 6, 29)
    assert (station.is_installed, station.is_renting, station.is_returning) == (True, True, True)
    assert station.last_reported.tzinfo is UTC


def test_status_keeps_closed_and_over_capacity_stations() -> None:
    """Anomalies are facts to flag later, not reasons to drop data."""
    snapshot = parse_station_status(fixture_bytes("station_status.json"), FETCHED_AT)

    closed = by_code(snapshot.stations, "8116")
    assert (closed.is_installed, closed.is_renting, closed.is_returning) == (True, False, False)
    assert by_code(snapshot.stations, "13123").bikes == 61  # declared capacity is 42


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("num_docks_available", -1),
        ("num_bikes_available", 99),  # no longer equals mechanical + ebike
        ("num_bikes_available_types", [{"mechanical": 4}, {"cargo": 2}]),
        ("is_renting", 2),
    ],
)
def test_status_rejects_only_the_invalid_station(field: str, value: Any) -> None:
    feed = fixture_json("station_status.json")
    feed["data"]["stations"][0][field] = value

    snapshot = parse_station_status(json.dumps(feed).encode(), FETCHED_AT)

    assert snapshot.rejected == 1
    assert len(snapshot.stations) == 8
    assert "16107" not in {s.station_code for s in snapshot.stations}


def test_status_rejection_logs_are_capped(caplog: pytest.LogCaptureFixture) -> None:
    """1,520 rejected stations must not mean 1,520 log lines."""
    feed = fixture_json("station_status.json")
    for station in feed["data"]["stations"]:
        station["stationCode"] = None

    snapshot = parse_station_status(json.dumps(feed).encode(), FETCHED_AT)

    assert snapshot.rejected == 9
    assert caplog.text.count("rejected station") == 3
    assert "6 more stations rejected" in caplog.text


def test_status_accepts_standard_last_updated_key() -> None:
    feed = fixture_json("station_status.json")
    feed["last_updated"] = feed.pop("lastUpdatedOther")

    snapshot = parse_station_status(json.dumps(feed).encode(), FETCHED_AT)

    assert snapshot.feed_updated_at == datetime(2026, 11, 4, 15, 48, 48, tzinfo=UTC)


@pytest.mark.parametrize(
    "content",
    [
        b"<html>maintenance</html>",
        b'{"data": {"stations": []}}',  # no timestamp
        b'{"lastUpdatedOther": 1, "data": {}}',  # no station list
        b'{"lastUpdatedOther": 1, "data": {"stations": "oops"}}',
    ],
)
def test_status_unusable_envelope_raises(content: bytes) -> None:
    with pytest.raises(FeedFormatError):
        parse_station_status(content, FETCHED_AT)


# --------------------------------------------------------------------------
# station_information and gbfs.json
# --------------------------------------------------------------------------


def test_information_fixture_parses_every_station() -> None:
    snapshot = parse_station_information(fixture_bytes("station_information.json"), FETCHED_AT)

    capacities = {s.station_code: s.capacity for s in snapshot.stations}
    assert snapshot.rejected == 0
    assert capacities["16107"] == 35
    assert capacities["8017"] == 0
    assert capacities["60001"] == 410


def test_feed_urls_are_discovered() -> None:
    urls = parse_feed_urls(fixture_bytes("gbfs.json"))

    assert urls["station_status"].endswith("/station_status.json")
    assert set(urls) >= {"station_status", "station_information"}


def test_feed_urls_invalid_file_raises() -> None:
    with pytest.raises(FeedFormatError):
        parse_feed_urls(b'{"lastUpdatedOther": 1, "data": {}}')


# --------------------------------------------------------------------------
# HTTP client, with a simulated server (no network)
# --------------------------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def fake_velib(calls: list[str]) -> Handler:
    """Simulated server answering with the recorded fixtures."""
    files = {
        "gbfs.json": "gbfs.json",
        "station_status.json": "station_status.json",
        "station_information.json": "station_information.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        calls.append(name)
        if name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=fixture_bytes(files[name]))

    return handler


def make_client(handler: Handler) -> GbfsClient:
    return GbfsClient(
        ENTRY_POINT, user_agent="VeloFlow-test", transport=httpx.MockTransport(handler)
    )


def test_client_fetches_raw_bytes_and_discovers_once() -> None:
    calls: list[str] = []
    with make_client(fake_velib(calls)) as client:
        first = client.fetch("station_status")
        client.fetch("station_status")

    assert first.content == fixture_bytes("station_status.json")
    assert first.fetched_at.tzinfo is UTC
    # gbfs.json is read once, then cached.
    assert calls == ["gbfs.json", "station_status.json", "station_status.json"]


def test_client_sends_its_user_agent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, content=fixture_bytes("gbfs.json"))

    with make_client(handler) as client:
        client.feed_urls()

    assert seen == ["VeloFlow-test"]


def test_client_forget_feed_urls_triggers_new_discovery() -> None:
    calls: list[str] = []
    with make_client(fake_velib(calls)) as client:
        client.feed_urls()
        client.forget_feed_urls()
        client.feed_urls()

    assert calls == ["gbfs.json", "gbfs.json"]


def test_client_unknown_feed_raises_format_error() -> None:
    with make_client(fake_velib([])) as client, pytest.raises(FeedFormatError):
        client.fetch("free_bike_status")


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(503),
        httpx.ReadTimeout("simulated timeout"),
        httpx.ConnectError("simulated DNS failure"),
    ],
)
def test_client_network_failures_raise_unavailable(
    failure: httpx.Response | httpx.HTTPError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    with make_client(handler) as client, pytest.raises(FeedUnavailableError):
        client.feed_urls()
