"""Shared fixtures: recorded Vélib' and Open-Meteo responses and their variants."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
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


@pytest.fixture(scope="session")
def synthetic_status() -> pl.DataFrame:
    """Cleaned ``station_status`` rows: two stations, a version every 5 minutes.

    From 8 November 2026 12:00 UTC (13:00 Paris) to 10 November 23:55 UTC, so
    Paris days 9 and 10 are complete and 8 is not. Both stations change at
    every version; station 1 is empty every fifth version, station 2 full.
    """

    from veloflow.quality.versions import with_operational
    from veloflow.storage.compaction import STATUS_SCHEMA

    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    rows = []
    for k in range(int((datetime(2026, 11, 11, tzinfo=UTC) - start).total_seconds() // 300)):
        t = start + k * timedelta(minutes=5)
        rows.append((t, t, 1, k % 5, 0, 10 - k % 5, True, True, True, t))
        rows.append((t, t, 2, 1 + k % 3, k % 2, (k + 1) % 4, True, True, True, t))
    return with_operational(pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row"))


@pytest.fixture
def silver_dir(synthetic_status: pl.DataFrame, tmp_path: Path) -> Path:
    """A data folder holding ``synthetic_status`` as silver files, one per UTC day."""
    frame = synthetic_status.drop("operational").with_columns(day=pl.col("fetched_at").dt.date())
    for (day,), part in frame.group_by("day"):
        folder = tmp_path / "silver" / "station_status" / f"date={day}"
        folder.mkdir(parents=True)
        part.drop("day").write_parquet(folder / "station_status.parquet")
    return tmp_path


# --- Monitoring block of the forecast document (Admin page) -----------------

MONITOR_NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
_TARGET = {"mae": 0.3, "mae_persistence": 0.4, "coverage": 0.83}
_EVENT = {"brier": 0.051, "brier_persistence": 0.08, "rate": 0.06}


def _status_block(**changes: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "collector": {
            "last_fetch": (MONITOR_NOW - timedelta(minutes=3)).isoformat(),
            "fetches_last_hour": 121,
            "fetches_ok_last_hour": 118,
            "expected_last_hour": 120,
            "stations": 1496,
            "stations_operational": 1460,
        },
        "weather": {"fetched_at": None},
        "live": {
            "since": "2026-11-12T22:30:00+00:00",
            "interval_level": 0.8,
            "horizons": {
                "15": {
                    "pairs": 50_000,
                    "targets": {"mechanical": _TARGET, "docks": {**_TARGET, "mae": 0.5}},
                    "empty": _EVENT,
                    "full": _EVENT,
                },
                "60": {
                    "pairs": 10,
                    "targets": {"ebike": {"mae": None, "mae_persistence": None, "coverage": None}},
                    "empty": {"brier": None, "brier_persistence": None, "rate": None},
                    "full": _EVENT,
                },
            },
        },
        "models": [
            {
                "name": "lightgbm",
                "trained_at": "2026-11-11T23:46:01",
                "data_start": "2026-11-05T00:00:00",
                "data_end": "2026-11-10T23:59:55",
                "examples": 1234567,
                "package_version": "0.1.0",
                "in_service_from": "2026-11-12T22:30:00+00:00",
            }
        ],
        "alerts": [],
        "rules": {"live_window_hours": 24, "min_pairs": 1000, "coverage_tolerance": 0.05},
    }
    block.update(changes)
    return block


@pytest.fixture
def status_block() -> Callable[..., dict[str, Any]]:
    """A monitoring block as ``veloflow-predict`` writes it, measured at 09:00 UTC.

    Keywords replace whole sections (``alerts=[...]``, ``live={...}``).
    """
    return _status_block
