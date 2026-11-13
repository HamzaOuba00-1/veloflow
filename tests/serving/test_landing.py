"""Tests for the landing page: every figure comes from the forecast document."""

import base64
from pathlib import Path
from typing import Any

import pytest

from veloflow.serving import theme
from veloflow.serving.landing import (
    FEATURES,
    cta_background_html,
    demo_card_html,
    feature_html,
    footer_html,
    hero_text_html,
    hero_visual_html,
    network_map_uri,
    steps_html,
    story_html,
)


def station(station_id: int, lat: float, lon: float, p_empty: float) -> dict[str, Any]:
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
        "code": "4001",
        "name": f"Station <{station_id}>",
        "lat": lat,
        "lon": lon,
        "capacity": 10,
        "now": {"mechanical": 2, "ebike": 1, "docks": 5},
        "forecasts": {"10": forecast, "40": forecast},
    }


STATIONS = [station(1, 48.85, 2.30, 0.05), station(2, 48.87, 2.40, 0.7)]
DOCUMENT = {
    "origin": "2026-11-12T08:00:00+00:00",
    "model": {"name": "lightgbm", "data_end": "2026-11-11T23:59:00"},
    "stations": STATIONS,
}


def test_network_map_draws_every_station() -> None:
    uri = network_map_uri(STATIONS, "bike", "10")
    svg = base64.b64decode(uri.split(",", 1)[1]).decode()

    assert uri.startswith("data:image/svg+xml;base64,")
    assert svg.count("<circle") == 2
    assert network_map_uri(STATIONS, "bike", "99") == ""  # no forecast at that horizon


def test_hero_reads_the_document() -> None:
    text = hero_text_html(DOCUMENT, "ok")
    assert "every one of the 2 Vélib' stations, 10 and 40 minutes ahead" in text
    assert "updated 09:00" in text  # Paris time
    assert "Delayed data" in hero_text_html(DOCUMENT, "stale")
    visual = hero_visual_html(network_map_uri(STATIONS, "bike", "40"), STATIONS, "40")
    assert "average chance of a bike in 40 min" in visual
    assert "62%" in visual  # (0.95 + 0.3) / 2


def test_story_falls_back_to_live_figures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(theme, "STATIC", tmp_path)

    story = story_html(STATIONS)
    assert "IN 40 MIN" in story  # the last horizon of the document
    assert "of stations have a bike" in story
    assert cta_background_html() == ""

    (tmp_path / "images" / "landing-ride.webp").write_bytes(b"RIFF")
    (tmp_path / "images" / "landing-cta.webp").write_bytes(b"RIFF")
    assert "app/static/images/landing-ride.webp" in story_html(STATIONS)
    assert "app/static/images/landing-cta.webp" in cta_background_html()


def test_features_steps_demo_and_footer() -> None:
    assert set(FEATURES) == {"Map", "Go now", "At risk", "Station"}  # real pages only
    assert "Live forecast map" in feature_html("Map")
    assert "5, 10 and 15 minutes on foot" in steps_html((5, 10, 15))
    demo = demo_card_html(STATIONS[1], "10")
    assert "chance of a bike in 10 min" in demo
    assert "&lt;2&gt;" in demo  # names from the feed are escaped
    footer = footer_html(DOCUMENT, {"Home": "home", "Map": "map", "Admin": "admin"})
    assert 'href="?page=map"' in footer
    assert "Service health" in footer
    assert "trained on data up to 2026-11-11 23:59 UTC" in footer
