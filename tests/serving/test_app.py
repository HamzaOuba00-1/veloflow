"""Tests for the web app as a whole: every page renders, headless."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from veloflow.serving.app import PAGES

APP = str(Path(__file__).parents[2] / "src" / "veloflow" / "serving" / "app.py")


def station(station_id: int, name: str, lat: float, p_empty: float) -> dict[str, Any]:
    forecast = {
        "mechanical": 3.0,
        "ebike": 1.0,
        "docks": 4.0,
        "mechanical_interval": [2, 4],
        "ebike_interval": [0, 2],
        "docks_interval": [3, 5],
        "p_empty": p_empty,
        "p_full": 0.1,
    }
    return {
        "station_id": station_id,
        "code": "1001",
        "name": name,
        "lat": lat,
        "lon": 2.347,
        "capacity": 10,
        "now": {"mechanical": 2, "ebike": 1, "docks": 5},
        "forecasts": {h: forecast for h in ("15", "30", "60")},
    }


@pytest.fixture
def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    document = {
        "generated_at": datetime.now(UTC).isoformat(),
        "origin": "2026-11-12T08:00:00+00:00",
        "model": {"name": "lightgbm", "trained_at": "x", "data_end": "2026-11-11T23:59:00"},
        "stations": [
            station(1, "Châtelet", 48.8584, 0.05),
            station(2, "Bastille", 48.8532, 0.7),
        ],
    }
    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(path))
    return AppTest.from_file(APP, default_timeout=30).run()


def html_of(page: AppTest) -> str:
    return "".join(str(getattr(element, "value", "")) for element in page.get("html"))


def test_map_page_opens_on_the_central_station(page: AppTest) -> None:
    assert not page.exception
    html = html_of(page)
    assert "Vélib&#x27; station forecasts" in html or "Vélib' station forecasts" in html
    assert "Châtelet" in html
    assert "Live data" in html


@pytest.mark.parametrize("name", list(PAGES))
def test_every_page_renders(page: AppTest, name: str) -> None:
    page.sidebar.radio[0].set_value(name).run()

    assert not page.exception
    assert html_of(page)


def test_go_now_suggests_a_station_and_directions(page: AppTest) -> None:
    page.sidebar.radio[0].set_value("Go now").run()

    assert "chance of a bike in 15 min" in html_of(page)
    assert page.get("link_button")


def test_at_risk_lists_the_riskiest_first(page: AppTest) -> None:
    page.sidebar.radio[0].set_value("At risk").run()

    html = html_of(page)
    assert html.index("Bastille") < html.index("Châtelet")


def test_search_opens_the_station_page(page: AppTest) -> None:
    page.selectbox(key="search").set_value("Bastille").run()

    assert page.session_state["page"] == "Station"
    assert page.session_state["station"] == "Bastille"
    assert "High risk of no bike" in html_of(page)


def test_missing_forecasts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(tmp_path / "missing.json"))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert "unavailable" in page.error[0].value
