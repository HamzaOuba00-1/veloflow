"""Tests for the web app as a whole: every page renders, headless."""

import json
import os
from collections.abc import Callable
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


def test_the_map_card_opens_the_station_page(page: AppTest) -> None:
    page.sidebar.radio[0].set_value("Station").run()
    page.selectbox[0].set_value("Bastille").run()
    page.sidebar.radio[0].set_value("Map").run()

    page.button[0].click().run()

    assert page.session_state["page"] == "Station"
    assert page.session_state["station"] == "Bastille"
    assert "High risk of no bike" in html_of(page)


def test_no_search_bar(page: AppTest) -> None:
    assert "search" not in page.session_state
    assert all(box.key != "search" for box in page.selectbox)


def test_figures_follow_the_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Horizons, counts and the model card come from the document, not from texts."""
    forecast = station(1, "Châtelet", 48.8584, 0.05)["forecasts"]["15"]
    stations = [station(i, name, 48.85 + i / 1000, 0.3) for i, name in enumerate("ABC")]
    for s in stations:
        s["forecasts"] = {"10": forecast, "20": forecast}
    document = {
        "generated_at": datetime.now(UTC).isoformat(),
        "origin": "2026-11-12T08:00:00+00:00",
        "model": {
            "name": "lightgbm",
            "trained_at": "2026-11-12T00:00:00+00:00",
            "data_start": "2026-11-05T00:00:00+00:00",
            "data_end": "2026-11-11T23:59:00+00:00",
            "examples": 123456,
            "interval_level": 0.9,
        },
        "stations": stations,
    }
    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(path))
    page = AppTest.from_file(APP, default_timeout=30).run()

    page.sidebar.radio[0].set_value("Go now").run()
    assert "chance of a bike in 10 min" in html_of(page)
    page.sidebar.radio[0].set_value("At risk").run()
    assert "The 3 stations most likely to be empty in 10 minutes." in html_of(page)
    page.sidebar.radio[0].set_value("About").run()
    about = html_of(page)
    assert "each of the 3 Vélib" in about
    assert "10 and 20 minutes ahead" in about
    assert "123,456 examples from 05 Nov 2026 00:00 UTC to 11 Nov 2026 23:59 UTC" in about
    assert "90% prediction interval" in about


def test_missing_forecasts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(tmp_path / "missing.json"))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert "unavailable" in page.error[0].value


def run_on(document: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(path))
    return AppTest.from_file(APP, default_timeout=30).run()


@pytest.mark.parametrize("name", ["Go now", "Station", "Map"])
def test_a_station_gone_from_the_document(page: AppTest, name: str) -> None:
    """The chosen station left the feed between two updates: back to a known one."""
    page.session_state["station"] = "Closed since the last update"
    page.sidebar.radio[0].set_value(name).run()

    assert not page.exception
    assert page.session_state["station"] in {"Châtelet", "Bastille"}


@pytest.mark.parametrize(
    "document",
    [{"stations": []}, {"origin": "2026-11-12T08:00:00+00:00"}, [], {"stations": "x"}],
    ids=["no-station", "no-stations-key", "not-an-object", "stations-not-a-list"],
)
def test_an_unusable_document_is_said_not_raised(
    document: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = run_on(document, tmp_path, monkeypatch)

    assert not page.exception
    assert "unavailable" in page.error[0].value


def test_an_unreachable_url_is_said_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", "http://127.0.0.1:9/forecasts.json")
    page = AppTest.from_file(APP, default_timeout=30).run()

    assert not page.exception
    assert "unavailable" in page.error[0].value


def test_admin_page_without_monitoring(page: AppTest) -> None:
    page.sidebar.radio[0].set_value("Admin").run()

    assert not page.exception
    assert "no monitoring block" in page.info[0].value


def test_admin_page_shows_the_monitoring_block(
    page: AppTest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_block: Callable[..., dict[str, Any]],
) -> None:
    path = Path(os.environ["VELOFLOW_FORECASTS"])
    document = json.loads(path.read_text(encoding="utf-8"))
    folder = tmp_path / "admin"
    folder.mkdir()
    admin = run_on({**document, "status": status_block()}, folder, monkeypatch)
    admin.sidebar.radio[0].set_value("Admin").run()

    html = html_of(admin)
    assert not admin.exception
    assert "All checks pass" in html
    assert "118 / 120" in html
    assert "Real accuracy" in html
    assert "Models in service" in html
