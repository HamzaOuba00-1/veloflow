"""Shared fixtures: recorded Vélib' and Open-Meteo responses and their variants."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from veloflow.collector.gbfs import RawResponse

FIXTURES = Path(__file__).parent / "fixtures" / "gbfs"
WEATHER_FIXTURE = Path(__file__).parent / "fixtures" / "weather" / "historical_forecast_paris.json"
FETCHED_AT = datetime(2026, 11, 5, 8, 0, 0, tzinfo=UTC)


@pytest.fixture
def make_response() -> Callable[..., RawResponse]:
    """Build a RawResponse from a recorded feed, optionally edited.

    ``edit`` receives the decoded JSON and may change it in place.
    """

    def factory(
        name: str = "station_status.json",
        *,
        edit: Callable[[dict[str, Any]], None] | None = None,
        fetched_at: datetime = FETCHED_AT,
    ) -> RawResponse:
        feed: dict[str, Any] = json.loads((FIXTURES / name).read_bytes())
        if edit is not None:
            edit(feed)
        return RawResponse(content=json.dumps(feed).encode(), fetched_at=fetched_at)

    return factory


@pytest.fixture
def location() -> dict[str, Any]:
    """One location of a real Open-Meteo response: Paris, 2 November 2026, Météo-France."""
    loaded: dict[str, Any] = json.loads(WEATHER_FIXTURE.read_bytes())
    return loaded


@pytest.fixture
def body(location: dict[str, Any]) -> Callable[..., bytes]:
    """Encode ``count`` copies of the location the way the API answers.

    One location is a JSON object, several are a list.
    """

    def factory(count: int = 1) -> bytes:
        payload: Any = location if count == 1 else [location] * count
        return json.dumps(payload).encode()

    return factory
