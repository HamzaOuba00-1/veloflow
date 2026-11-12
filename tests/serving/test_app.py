"""Tests for the web app: the card builders, then the whole page, headless."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from veloflow.serving.app import (
    DASH,
    GREEN,
    NEUTRAL,
    ORANGE,
    RED,
    TONES,
    central_index,
    colour,
    critical_html,
    forecasts_html,
    header_html,
    legend_html,
    map_points,
    now_html,
    options_html,
    percent,
    risk_colour,
    station_rows,
    tone,
    verdict_html,
)


def station(station_id: int, lat: float, p_empty: float, name: str | None = None) -> dict[str, Any]:
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
        "name": name or f"Station {station_id}",
        "lat": lat,
        "lon": 2.3522,
        "capacity": 10,
        "now": {"mechanical": 2, "ebike": 1, "docks": 5},
        "forecasts": {"15": forecast, "60": forecast},
    }


STATIONS = [station(1, 48.8566, 0.6), station(2, 48.8576, 0.05)]


def document(generated: datetime) -> dict[str, Any]:
    return {
        "generated_at": generated.isoformat(),
        "origin": "2026-11-12T08:00:00+00:00",
        "model": {"name": "lightgbm", "trained_at": "x", "data_end": "2026-11-11T23:59:00"},
        "stations": STATIONS,
    }


def option(s: dict[str, Any], distance: float, probability: float) -> dict[str, Any]:
    return {"station": s, "distance_m": distance, "probability": probability}


def test_tones_and_colours() -> None:
    assert [tone(p) for p in (0.95, 0.8, 0.6, 0.2)] == ["good", "good", "fair", "poor"]
    assert (colour(0.95), colour(0.6), colour(0.2)) == (GREEN, ORANGE, RED)
    assert (risk_colour(0.7), risk_colour(0.3), risk_colour(0.05)) == (
        TONES["poor"],
        TONES["fair"],
        NEUTRAL,
    )


def test_percentages_never_claim_certainty() -> None:
    assert (percent(1.0), percent(0.996), percent(0.42), percent(0.004)) == (
        "> 99%",
        "> 99%",
        "42%",
        "< 1%",
    )


def test_map_points_for_a_bike_and_a_dock() -> None:
    bikes = map_points(STATIONS, "bike", "15")
    docks = map_points(STATIONS, "dock", "15")

    assert [p["chance"] for p in bikes] == ["40%", "95%"]
    assert bikes[0]["colour"] == RED
    assert bikes[0]["station_id"] == 1
    assert docks[0]["chance"] == "90%"
    assert map_points(STATIONS, "bike", "30") == []  # no 30-minute forecast here


def test_station_rows_show_intervals() -> None:
    rows = station_rows(STATIONS[0])

    assert [r["in (min)"] for r in rows] == [15, 60]
    assert rows[0]["mechanical"] == "2.0 [1-3]"
    assert rows[0]["P(empty)"] == "60%"


def test_the_app_starts_near_chatelet() -> None:
    by_name = {s["name"]: s for s in STATIONS}

    assert central_index(list(by_name), by_name) == 1  # Station 2 is the northern one


def test_header_says_live_or_delayed() -> None:
    live = header_html(document(datetime.now(UTC)), "ok", 120)
    late = header_html(document(datetime.now(UTC)), "stale", 45 * 60)

    assert "Live · 09:00" in live  # 08:00 UTC is 09:00 in Paris
    assert "2 stations" in live
    assert "vf-banner" not in live
    assert "Delayed" in late
    assert "45 minutes old" in late


def test_verdict_go_here_better_nearby_or_nothing() -> None:
    here = option(STATIONS[1], 0, 0.95)
    weak = option(STATIONS[0], 0, 0.4)
    better = option(STATIONS[1], 111, 0.95)

    assert "Go now" in verdict_html(here, here, "bike", "15")
    assert "you are here" in verdict_html(here, here, "bike", "15")
    assert "Better nearby" in verdict_html(weak, better, "bike", "15")
    assert "111 m away" in verdict_html(weak, better, "bike", "15")
    assert "No safe bet" in verdict_html(weak, None, "dock", "60")
    assert "a dock" in verdict_html(weak, None, "dock", "60")


def test_names_from_the_feed_are_escaped() -> None:
    hostile = station(3, 48.86, 0.9, name="<script>x</script>")

    assert "<script>" not in options_html([option(hostile, 50, 0.1)])
    assert "&lt;script&gt;" in critical_html([(0.9, hostile)], "empty")
    assert "<script>" not in verdict_html(option(hostile, 50, 0.9), None, "bike", "15")


def test_lists_and_cards() -> None:
    assert options_html([]) == ""
    assert "No station is at risk" in critical_html([], "empty")
    ranked = critical_html([(0.9, STATIONS[0]), (0.3, STATIONS[1])], "empty")
    assert ranked.index("Station 1") < ranked.index("Station 2")
    assert now_html(STATIONS[0]).count("vf-card") == 3
    cards = forecasts_html(STATIONS[0])
    assert cards.count("In 15 min") == 1
    assert "In 30 min" not in cards
    assert f"1{DASH}3" in cards  # interval chip with an en dash
    assert "a dock" in legend_html("dock")


@pytest.fixture
def document_path(tmp_path: Path) -> Path:
    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps(document(datetime.now(UTC))), encoding="utf-8")
    return path


APP = str(Path(__file__).parents[2] / "src" / "veloflow" / "serving" / "app.py")


def html_of(page: AppTest) -> str:
    return "".join(str(getattr(element, "value", "")) for element in page.get("html"))


def test_the_page_renders(document_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(document_path))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert not page.exception
    html = html_of(page)
    assert "VéloFlow" in html
    assert "Live" in html
    # "Go now" starts at Station 2, nearest to Châtelet: 95 %, go.
    assert "Go now" in html
    assert "you are here" in html


def test_switching_to_docks_updates_the_page(
    document_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(document_path))
    page = AppTest.from_file(APP, default_timeout=30).run()

    page.radio[0].set_value("Dock").run()

    assert not page.exception
    assert "chance of a dock" in html_of(page)
    assert "most likely to be <b>full</b>" in html_of(page)


def test_the_page_says_when_forecasts_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VELOFLOW_FORECASTS", str(tmp_path / "missing.json"))

    page = AppTest.from_file(APP, default_timeout=30).run()

    assert "unavailable" in page.error[0].value
