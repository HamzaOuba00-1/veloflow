"""Shared helpers for collector tests: recorded responses and their variants."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from veloflow.collector.gbfs import RawResponse

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gbfs"
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
