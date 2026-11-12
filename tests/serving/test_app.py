"""Tests for the web app: its helpers, then the whole page, headless."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from veloflow.serving.app import (
    GREEN,
    ORANGE,
    RED,
    central_index,
    colour,
    map_points,
    percent,
    station_rows,
)


def station(station_id: int, lat: float, p_empty: float) -> dict[str, Any]:
    forecast = {
        "mechanical": 2.0,
        "ebike": 1.0,
        "docks": 5.0,
        "mechanical_interval": [1, 3],
        "ebike_interval": [0, 2],
        "docks_interval": [4, 6],
        "p_empty": p_empty,
        "p_full": 0.1,
    }
    return {
        "station_id": station_id,
        "code": "1001",
        "name": f"Station {station_id}",
        "lat": lat,
        "lon": 2.3522,
        "capacity": 10,
        "now": {"mechanical": 2, "ebike": 1, "docks": 5},
        "forecasts": {"15": forecast, "60": forecast},
    }


STATIONS = [station(1, 48.8566, 0.6), station(2, 48.8576, 0.05)]


def test_colours_follow_the_chance_of_success() -> None:
    assert (colour(0.95), colour(0.8), colour(0.6), colour(0.2)) == (GREEN, GREEN, ORANGE, RED)


def test_map_points_for_a_bike_and_a_dock() -> None:
    bikes = map_points(STATIONS, "bike", "15")
    docks = map_points(STATIONS, "dock", "15")

    assert [p["chance"] for p in bikes] == ["40%", "95%"]
    assert bikes[0]["colour"] == RED
    assert docks[0]["chance"] == "90%"
    assert map_points(STATIONS, "bike", "30") == []  # no 30-minute forecast here


def test_station_rows_show_intervals() -> None:
    rows = station_rows(STATIONS[0])

    assert [r["in (min)"] for r in rows] == [15, 60]
    assert rows[0]["mechanical"] == "2.0 [1-3]"
    assert rows[0]["P(empty)"] == "60%"


@pytest.fixture
def document_path(tmp_path: Path) -> Path:
    path = tmp_path / "forecasts.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "origin": "2026-11-12T08:00:00+00:00",
                "model": {"name": "lightgbm", "trained_at": "x", "data_end": "2026-11-11"},
                "stations": STATIONS,
            }
        ),
        encoding="utf-8",
    )
    return path


APP = str(Path(__file__).parents[2] / "src" / "veloflow" / "serving" / "app.py")


def test_the_page_renders(document_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(document_path))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert not page.exception
    assert page.title[0].value == "VéloFlow"
    assert "2 stations" in page.caption[0].value
    # "Go now?" starts at Station 2, nearest to Châtelet: 95 %, go.
    assert "Station 2" in page.success[0].value


def test_the_page_says_when_forecasts_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(tmp_path / "missing.json"))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert "unavailable" in page.error[0].value


def test_the_app_starts_near_chatelet() -> None:
    by_name = {s["name"]: s for s in STATIONS}

    assert central_index(list(by_name), by_name) == 1  # Station 2 is the northern one


def test_percentages_never_claim_certainty() -> None:
    assert (percent(1.0), percent(0.996), percent(0.42), percent(0.004)) == (
        "> 99%",
        "> 99%",
        "42%",
        "< 1%",
    )
