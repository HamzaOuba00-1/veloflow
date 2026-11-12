"""VéloFlow web app (Streamlit): map, "go now?", critical stations, station card.

Reads the same forecast document as the API, through the same questions
(``serving.queries``). Run with ``uv run veloflow-app`` (port ``$PORT``,
8501 by default) and point ``VELOFLOW_FORECASTS`` at the document.

Requirements covered: F1 (map of every station with its forecast), F2 ("go
now?" with alternatives), F3 (stations most at risk), F4 (station card with
intervals and probabilities).
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from veloflow.serving.api import DEFAULT_SOURCE, SOURCE_ENV, read_source
from veloflow.serving.queries import (
    NEED_EVENT,
    Need,
    best_option,
    critical,
    distance_m,
    freshness,
    options_near,
)

PARIS = ZoneInfo("Europe/Paris")
HORIZONS = ("15", "30", "60")
NEEDS: dict[str, Need] = {"a bike": "bike", "a dock": "dock"}
# The "go now?" tab starts at the station nearest to Châtelet.
CENTRE = (48.8584, 2.3470)
# Chance of success: green from 80 %, orange from 50 %, red below.
GREEN, ORANGE, RED = [26, 150, 65, 200], [245, 160, 30, 210], [215, 48, 39, 220]


def percent(probability: float) -> str:
    """A probability for people: never "100 %" or "0 %", which no forecast is."""
    if probability >= 0.995:
        return "> 99%"
    if probability < 0.005:
        return "< 1%"
    return f"{probability:.0%}"


def colour(probability: float) -> list[int]:
    """Map colour of a chance of success."""
    if probability >= 0.8:
        return GREEN
    if probability >= 0.5:
        return ORANGE
    return RED


def map_points(stations: list[dict[str, Any]], need: Need, horizon: str) -> list[dict[str, Any]]:
    """One point per station: position, chance of success, colour, tooltip text."""
    event = NEED_EVENT[need]
    points = []
    for s in stations:
        if horizon not in s["forecasts"]:
            continue
        chance = 1.0 - s["forecasts"][horizon][f"p_{event}"]
        now = s["now"]
        points.append(
            {
                "lat": s["lat"],
                "lon": s["lon"],
                "name": s["name"],
                "chance": percent(chance),
                "now": f"{now['mechanical']:.0f} mechanical, {now['ebike']:.0f} e-bikes, "
                f"{now['docks']:.0f} docks",
                "colour": colour(chance),
            }
        )
    return points


def central_index(names: list[str], by_name: dict[str, dict[str, Any]]) -> int:
    """Position in ``names`` of the station nearest to the centre of Paris."""
    return min(
        range(len(names)),
        key=lambda i: distance_m(*CENTRE, by_name[names[i]]["lat"], by_name[names[i]]["lon"]),
    )


def station_rows(station: dict[str, Any]) -> list[dict[str, Any]]:
    """Forecast table of a station: one row per horizon."""
    rows = []
    for horizon in HORIZONS:
        f = station["forecasts"].get(horizon)
        if f is None:
            continue
        rows.append(
            {
                "in (min)": int(horizon),
                "mechanical": f"{f['mechanical']:.1f} [{f['mechanical_interval'][0]:.0f}-"
                f"{f['mechanical_interval'][1]:.0f}]",
                "e-bikes": f"{f['ebike']:.1f} [{f['ebike_interval'][0]:.0f}-"
                f"{f['ebike_interval'][1]:.0f}]",
                "docks": f"{f['docks']:.1f} [{f['docks_interval'][0]:.0f}-"
                f"{f['docks_interval'][1]:.0f}]",
                "P(empty)": percent(f["p_empty"]),
                "P(full)": percent(f["p_full"]),
            }
        )
    return rows


def render() -> None:
    """Draw the app (called by Streamlit on every interaction)."""
    import pydeck as pdk
    import streamlit as st

    st.set_page_config(page_title="VéloFlow", page_icon="🚲", layout="wide")

    @st.cache_data(ttl=60, show_spinner=False)
    def load(source: str) -> dict[str, Any]:
        return read_source(source)

    try:
        document = load(os.environ.get(SOURCE_ENV, DEFAULT_SOURCE))
    except (OSError, ValueError) as error:
        st.error(f"Forecasts are unavailable right now ({error}).")
        return
    stations: list[dict[str, Any]] = document["stations"]
    status, age = freshness(document, datetime.now(UTC))
    origin = datetime.fromisoformat(document["origin"]).astimezone(PARIS)

    st.title("VéloFlow")
    st.caption(
        f"Vélib' Métropole forecasts at 15, 30 and 60 minutes · state at "
        f"{origin:%H:%M} (Paris), {age / 60:.0f} min ago · {len(stations):,} stations"
    )
    if status == "stale":
        st.warning("These forecasts are more than 30 minutes old: the feed may be late.")

    need = NEEDS[st.sidebar.radio("I am looking for", list(NEEDS))]
    horizon = st.sidebar.radio("In (minutes)", HORIZONS, index=0)
    st.sidebar.caption(
        "Chance of success = 1 - P(empty) for a bike, 1 - P(full) for a dock. "
        "Green from 80 %, orange from 50 %."
    )
    by_name = {s["name"]: s for s in sorted(stations, key=lambda s: s["name"])}

    tab_map, tab_go, tab_critical, tab_station = st.tabs(
        ["Map", "Go now?", "Critical stations", "Station"]
    )
    with tab_map:
        layer = pdk.Layer(
            "ScatterplotLayer",
            data=map_points(stations, need, horizon),
            get_position="[lon, lat]",
            get_fill_color="colour",
            get_radius=45,
            pickable=True,
        )
        view = pdk.ViewState(latitude=48.86, longitude=2.34, zoom=11.5)
        st.pydeck_chart(
            pdk.Deck(
                layers=[layer],
                initial_view_state=view,
                tooltip={"text": "{name}\n{chance} chance\nnow: {now}"},
            )
        )
    with tab_go:
        names = list(by_name)
        here = by_name[
            st.selectbox("I am near", names, index=central_index(names, by_name), key="here")
        ]
        radius = st.slider("Walking distance (m)", 200, 1500, 500, 100)
        options = options_near(stations, here["lat"], here["lon"], need, horizon, radius)
        best = best_option(options, 0.8)
        nearest = options[0] if options else None
        if nearest and nearest["probability"] >= 0.8:
            st.success(
                f"Go: {nearest['station']['name']} has a {percent(nearest['probability'])} chance."
            )
        elif best:
            st.info(
                f"{best['station']['name']} ({best['distance_m']:.0f} m) is a better bet: "
                f"{percent(best['probability'])} chance."
            )
        else:
            st.warning("No station within reach has an 80 % chance.")
        st.dataframe(
            [
                {
                    "station": o["station"]["name"],
                    "distance (m)": o["distance_m"],
                    "chance": percent(o["probability"]),
                }
                for o in options
            ],
            hide_index=True,
        )
    with tab_critical:
        event = NEED_EVENT[need]
        st.subheader(f"Most likely to be {event} in {horizon} minutes")
        st.dataframe(
            [
                {
                    "station": s["name"],
                    f"P({event})": percent(p),
                    "mechanical": s["now"]["mechanical"],
                    "e-bikes": s["now"]["ebike"],
                    "docks": s["now"]["docks"],
                }
                for p, s in critical(stations, event, horizon, limit=20)
            ],
            hide_index=True,
        )
    with tab_station:
        station = by_name[st.selectbox("Station", list(by_name), key="station")]
        now = station["now"]
        columns = st.columns(3)
        columns[0].metric("Mechanical bikes", f"{now['mechanical']:.0f}")
        columns[1].metric("E-bikes", f"{now['ebike']:.0f}")
        columns[2].metric("Free docks", f"{now['docks']:.0f}")
        st.dataframe(station_rows(station), hide_index=True)
        st.caption("Brackets: 80 % intervals. Probabilities are calibrated on past forecasts.")
    st.caption(
        "Data: Vélib' Métropole (ODbL), weather Open-Meteo (CC BY 4.0). "
        f"Model {document['model']['name']} trained on data up to {document['model']['data_end']}."
    )


def main() -> None:  # pragma: no cover - starts a server
    """Serve the app on ``$PORT`` (Cloud Run sets it), 8501 by default."""
    from streamlit.web import cli

    sys.argv = [
        "streamlit",
        "run",
        str(Path(__file__)),
        "--server.port",
        os.environ.get("PORT", "8501"),
        "--server.address",
        "0.0.0.0",  # all interfaces: inside a container
        "--server.headless",
        "true",
    ]
    sys.exit(cli.main())


if __name__ == "__main__":
    render()
