"""Tests for the building blocks of the web app (figures and HTML cards)."""

import base64
from typing import Any

import pytest

from veloflow.serving.ui import (
    DASH,
    GREEN,
    NEUTRAL,
    ORANGE,
    RED,
    TONES,
    brand_html,
    chance,
    colour,
    count_cards_html,
    directions_url,
    district,
    horizon_table_html,
    icon,
    insight,
    kpi_html,
    legend_html,
    map_points,
    nearest_index,
    network_figures_html,
    network_summary,
    network_trend,
    now_compact_html,
    now_tiles_html,
    options_html,
    page_head_html,
    percent,
    risk_cards_html,
    risk_colour,
    risk_table_html,
    sidebar_status_html,
    smart_tip,
    station_head_html,
    station_trend,
    tip_html,
    tone,
    topbar_html,
)


def station(
    station_id: int,
    lat: float,
    p_empty: float,
    *,
    p_full: float = 0.1,
    name: str | None = None,
    code: str = "4012",
    now: tuple[int, int, int] = (2, 1, 5),
) -> dict[str, Any]:
    forecast = {
        "mechanical": 3.0,
        "ebike": 1.0,
        "docks": 4.0,
        "mechanical_interval": [2, 4],
        "ebike_interval": [0, 2],
        "docks_interval": [3, 5],
        "p_empty": p_empty,
        "p_full": p_full,
    }
    return {
        "station_id": station_id,
        "code": code,
        "name": name or f"Station {station_id}",
        "lat": lat,
        "lon": 2.3522,
        "capacity": 10,
        "now": dict(zip(("mechanical", "ebike", "docks"), now, strict=True)),
        "forecasts": {"15": forecast, "60": forecast},
    }


STATIONS = [station(1, 48.8566, 0.6), station(2, 48.8576, 0.05, now=(0, 0, 9))]


def test_tones_colours_and_percentages() -> None:
    assert [tone(p) for p in (0.95, 0.8, 0.6, 0.2)] == ["good", "good", "fair", "poor"]
    assert (colour(0.95), colour(0.6), colour(0.2)) == (GREEN, ORANGE, RED)
    assert (risk_colour(0.7), risk_colour(0.3), risk_colour(0.05)) == (
        TONES["poor"],
        TONES["fair"],
        NEUTRAL,
    )
    assert (percent(1.0), percent(0.42), percent(0.004)) == ("> 99%", "42%", "< 1%")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("1116", "Paris 1er"),
        ("4012", "Paris 4e"),
        ("20205", "Paris 20e"),
        ("60001", "Paris 7e"),
        ("60002", "Paris 8e"),
        ("92004", "Métropole"),
        (None, "Métropole"),
        ("abc", "Métropole"),
    ],
)
def test_district(code: str | None, expected: str) -> None:
    assert district(code) == expected


def test_icons_are_data_images() -> None:
    html = icon("bike", 24, "#123456")

    assert html.startswith('<img class="vf-icon" src="data:image/svg+xml;base64,')
    encoded = html.split("base64,")[1].split('"')[0]
    assert 'stroke="#123456"' in base64.b64decode(encoded).decode()


def test_network_figures() -> None:
    summary = network_summary(STATIONS)

    assert summary["stations"] == 2
    assert summary["mechanical"] == 2
    assert summary["with_bike"] == 0.5
    trend = network_trend(STATIONS)
    assert [label for label, _ in trend] == ["Now", "15 min", "60 min"]
    assert trend[1][1] == pytest.approx((0.4 + 0.95) / 2)
    assert network_trend([]) == []
    assert network_summary([])["with_bike"] == 0.0


def test_station_trend_and_chance() -> None:
    assert station_trend(STATIONS[0], "docks") == [("Now", 5.0), ("15 min", 4.0), ("60 min", 4.0)]
    assert chance(STATIONS[0], "bike", "15") == pytest.approx(0.4)
    assert chance(STATIONS[0], "dock", "15") == pytest.approx(0.9)


def test_smart_tip_points_at_the_worst_district() -> None:
    risky = [
        station(1, 48.85, 0.7, code="11001"),
        station(2, 48.85, 0.8, code="11002"),
        station(3, 48.85, 0.9, code="4001"),
    ]

    assert smart_tip(risky, "bike", "15") == (
        "2 stations are likely to run out of bikes in Paris 11e within 15 minutes."
    )
    assert "1 station is likely to fill up" in smart_tip(
        [station(4, 48.85, 0.0, p_full=0.9, code="4001")], "dock", "15"
    )
    assert smart_tip(STATIONS[1:], "dock", "60") == (
        "No district is short of free docks in the next 60 minutes."
    )


@pytest.mark.parametrize(
    ("p_empty", "p_full", "words", "mood"),
    [
        (0.7, 0.0, "High risk of no bike", "poor"),
        (0.0, 0.6, "High risk of no free dock", "poor"),
        (0.3, 0.1, "Some risk of the station being empty", "fair"),
        (0.1, 0.25, "Some risk of the station being full", "fair"),
        (0.05, 0.05, "Low risk", "good"),
    ],
)
def test_insight(p_empty: float, p_full: float, words: str, mood: str) -> None:
    text, tone_name = insight(station(1, 48.85, p_empty, p_full=p_full), "15")

    assert words in text
    assert tone_name == mood


def test_nearest_index_and_directions() -> None:
    by_name = {s["name"]: s for s in STATIONS}

    assert nearest_index(list(by_name), by_name, 48.8577, 2.3522) == 1
    assert directions_url(48.8566, 2.3522) == (
        "https://www.google.com/maps/dir/?api=1&destination=48.856600,2.352200&travelmode=walking"
    )


def test_map_points() -> None:
    bikes = map_points(STATIONS, "bike", "15")

    assert [p["chance"] for p in bikes] == ["40%", "95%"]
    assert bikes[0]["colour"] == RED
    assert map_points(STATIONS, "dock", "15")[0]["chance"] == "90%"
    assert map_points(STATIONS, "bike", "30") == []


def test_cards_render_the_figures() -> None:
    cards = count_cards_html(STATIONS[0], "15")
    assert cards.count('vf-count"') == 3
    assert f"likely 2{DASH}4" in cards
    risks = risk_cards_html(STATIONS[0], "15")
    assert "60%" in risks
    assert "Station full" in risks
    tiles = now_tiles_html(STATIONS[0])
    assert "+1 in 15 min" in tiles  # mechanical 2 now, 3 forecast
    assert "+0 in 15 min" in tiles  # e-bikes 1 and 1
    assert "-1 in 15 min" in tiles  # docks 5 now, 4 forecast
    assert now_compact_html(STATIONS[0]).count('vf-mini"') == 3
    assert "3 / 1 / 4" in horizon_table_html(STATIONS[0])
    assert "Paris 4e · 120 m" in station_head_html(STATIONS[0], " · 120 m")


def test_page_parts() -> None:
    document = {"origin": "2026-11-12T08:00:00+00:00", "stations": STATIONS}

    assert "Live data" in topbar_html(document, "ok", 60)
    assert "Updated 09:00" in topbar_html(document, "ok", 60)
    assert "31 min old" in topbar_html(document, "stale", 31 * 60)
    assert "Delayed data" in sidebar_status_html(document, "stale")
    assert "VéloFlow" in brand_html()
    assert "Map" in page_head_html("map", "Title", "Subtitle", eyebrow="Map")
    assert "1,486" in kpi_html("bike", "#000000", "1,486", "Stations", note="today")
    assert "Bikes" in tip_html("Bikes", "text", "good")
    assert "12" in network_figures_html({"mechanical": 12, "ebike": 3, "docks": 7})
    assert legend_html(("a", "b", "c")).count("<i ") == 3


def test_tables_and_lists() -> None:
    table = risk_table_html([(0.9, STATIONS[0])], "15", "empty")
    assert "Top 1 at-risk stations" in table
    assert "Paris 4e" in table
    assert "No station is likely to be full" in risk_table_html([], "30", "full")
    options = options_html(
        [{"station": STATIONS[1], "distance_m": 160.0, "probability": 0.95}], "dock"
    )
    assert "~2 min walk" in options
    assert "95% dock" in options
    assert "No station within" in options_html([], "bike")


def test_names_from_the_feed_are_escaped() -> None:
    hostile = station(3, 48.86, 0.9, name="<script>x</script>")

    for html in (
        station_head_html(hostile),
        risk_table_html([(0.9, hostile)], "15", "empty"),
        options_html([{"station": hostile, "distance_m": 10.0, "probability": 0.1}], "bike"),
    ):
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
