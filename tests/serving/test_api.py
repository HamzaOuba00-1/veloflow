"""Tests for the public API, on a small forecast document."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from veloflow.serving.api import ForecastStore, create_app, distance_m, read_source


def forecast(p_empty: float, p_full: float) -> dict[str, Any]:
    return {
        "mechanical": 2.0,
        "ebike": 1.0,
        "docks": 5.0,
        "mechanical_interval": [1, 3],
        "ebike_interval": [0, 2],
        "docks_interval": [4, 6],
        "p_empty": p_empty,
        "p_full": p_full,
    }


def station(
    station_id: int, lat: float, lon: float, p_empty: float, p_full: float
) -> dict[str, Any]:
    return {
        "station_id": station_id,
        "code": str(1000 + station_id),
        "name": f"Station {station_id}",
        "lat": lat,
        "lon": lon,
        "capacity": 10,
        "now": {"mechanical": 2, "ebike": 1, "docks": 5},
        "forecasts": {h: forecast(p_empty, p_full) for h in ("15", "30", "60")},
    }


def document(generated: datetime) -> dict[str, Any]:
    # Station 1 is at the point; 2 is ~110 m north; 3 is ~1.1 km north.
    return {
        "generated_at": generated.isoformat(),
        "origin": "2026-11-12T08:00:00+00:00",
        "model": {"name": "lightgbm", "trained_at": "x", "data_end": "y"},
        "stations": [
            station(1, 48.8566, 2.3522, p_empty=0.6, p_full=0.0),
            station(2, 48.8576, 2.3522, p_empty=0.05, p_full=0.3),
            station(3, 48.8666, 2.3522, p_empty=0.0, p_full=0.9),
        ],
    }


@pytest.fixture
def client() -> TestClient:
    fresh = document(datetime.now(UTC))
    return TestClient(create_app(ForecastStore("memory", reader=lambda _: fresh)))


def test_health_reports_freshness(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["stations"] == 3
    assert body["model"]["name"] == "lightgbm"


def test_stale_forecasts_are_flagged() -> None:
    old = document(datetime.now(UTC) - timedelta(hours=1))
    app = create_app(ForecastStore("memory", reader=lambda _: old))

    assert TestClient(app).get("/health").json()["status"] == "stale"


def test_station_and_missing_station(client: TestClient) -> None:
    assert client.get("/stations").status_code == 200
    assert len(client.get("/stations").json()) == 3
    body = client.get("/stations/2").json()
    assert body["forecasts"]["60"]["p_full"] == 0.3
    assert client.get("/stations/99").status_code == 404


def test_critical_stations_are_ranked(client: TestClient) -> None:
    empty = client.get("/critical", params={"event": "empty", "horizon": "30"}).json()
    full = client.get("/critical", params={"event": "full", "limit": 1}).json()

    assert [s["station_id"] for s in empty] == [1, 2, 3]
    assert [s["station_id"] for s in full] == [3]
    above = client.get("/critical", params={"min_probability": 0.5}).json()
    assert [s["station_id"] for s in above] == [1]
    assert client.get("/critical", params={"horizon": "45"}).status_code == 422


def test_recommend_a_bike_suggests_a_better_station_nearby(client: TestClient) -> None:
    body = client.get("/recommend", params={"lat": 48.8566, "lon": 2.3522}).json()

    # Nearest (station 1) has a 40 % chance of a bike: go to station 2 (95 %).
    assert body["nearest"]["station_id"] == 1
    assert body["nearest"]["probability"] == 0.4
    assert body["go_to_nearest"] is False
    assert body["best"]["station_id"] == 2
    assert [o["station_id"] for o in body["options"]] == [1, 2]  # 3 is beyond 500 m


def test_recommend_a_dock(client: TestClient) -> None:
    body = client.get(
        "/recommend", params={"lat": 48.8566, "lon": 2.3522, "need": "dock", "radius_m": 2000}
    ).json()

    assert body["go_to_nearest"] is True
    assert body["best"]["station_id"] == 1
    assert body["options"][2]["probability"] == pytest.approx(0.1)


def test_recommend_far_from_any_station(client: TestClient) -> None:
    body = client.get("/recommend", params={"lat": 48.6, "lon": 2.0}).json()

    assert body["nearest"] is None
    assert body["best"] is None
    assert body["options"] == []
    assert client.get("/recommend", params={"lat": 40.0, "lon": 2.3}).status_code == 422


def test_store_reloads_after_ttl_and_survives_failures() -> None:
    calls: list[int] = []
    now = [0.0]

    def reader(_: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("bucket unreachable")
        return document(datetime.now(UTC))

    store = ForecastStore("memory", ttl=60, reader=reader, clock=lambda: now[0])
    store.document()
    now[0] = 30
    store.document()
    assert len(calls) == 1  # cached
    now[0] = 61
    assert store.document()["origin"]  # refresh failed: previous document served
    now[0] = 62
    store.document()
    assert len(calls) == 3


def test_no_document_at_all_is_a_503() -> None:
    def failing(_: str) -> dict[str, Any]:
        raise OSError("missing")

    with pytest.raises(HTTPException) as error:
        ForecastStore("x", reader=failing).document()
    assert error.value.status_code == 503
    app = create_app(ForecastStore("x", reader=failing))
    assert TestClient(app).get("/health").status_code == 503


def test_read_source_from_a_file_and_a_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "forecasts.json"
    path.write_text('{"stations": []}', encoding="utf-8")
    assert read_source(str(path)) == {"stations": []}

    def fake_get(url: str, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"from": url}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert read_source("https://example.org/f.json") == {"from": "https://example.org/f.json"}


def test_distance() -> None:
    # 0.001 degree of latitude is about 111 m.
    assert distance_m(48.0, 2.0, 48.001, 2.0) == pytest.approx(111.2, abs=0.2)
