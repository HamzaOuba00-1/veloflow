"""VéloFlow web app (Streamlit): map, "go now?", critical stations, station card.

Reads the same forecast document as the API, through the same questions
(``serving.queries``). Run with ``uv run veloflow-app`` (port ``$PORT``,
8501 by default) and point ``VELOFLOW_FORECASTS`` at the document.

Requirements covered: F1 (map of every station with its forecast), F2 ("go
now?" with alternatives), F3 (stations most at risk), F4 (station card with
intervals and probabilities).

Design: a light, minimal interface (system fonts, Apple system colours,
white cards on a soft grey background, generous spacing). Controls sit at
the top rather than in a sidebar, which phones hide. The page builds its
cards from small pure functions returning HTML, so they can be tested
without a browser; every text from the data is escaped.
"""

import os
import sys
from datetime import UTC, datetime
from html import escape
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
NEEDS: dict[str, Need] = {"Bike": "bike", "Dock": "dock"}
# The "go now?" tab starts at the station nearest to Châtelet.
CENTRE = (48.8584, 2.3470)
GOOD, FAIR = 0.8, 0.5
# Apple system colours (light): green, orange, red; RGBA for the map.
GREEN, ORANGE, RED = [52, 199, 89, 230], [255, 159, 10, 230], [255, 59, 48, 230]
TONES = {"good": "#248a3d", "fair": "#c93400", "poor": "#d70015"}
NEUTRAL = "#8e8e93"
DASH = "\u2013"  # en dash, for ranges

THEME = {
    "base": "light",
    "primaryColor": "#0071e3",
    "backgroundColor": "#f5f5f7",
    "secondaryBackgroundColor": "#ffffff",
    "textColor": "#1d1d1f",
}

CSS = """
<style>
:root {
  --vf-bg: #f5f5f7; --vf-card: #ffffff; --vf-text: #1d1d1f; --vf-muted: #6e6e73;
  --vf-faint: #86868b; --vf-line: rgba(0, 0, 0, 0.08); --vf-accent: #0071e3;
  --vf-good: #34c759; --vf-fair: #ff9f0a; --vf-poor: #ff3b30;
  --vf-radius: 20px; --vf-ease: cubic-bezier(0.2, 0.8, 0.2, 1);
  --vf-shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 24px rgba(0, 0, 0, 0.06);
}
html, body, [class*="st-"], .stApp {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
[data-testid="stIconMaterial"], span[translate="no"] {
  font-family: "Material Symbols Rounded" !important; }
.stApp { background: var(--vf-bg); }
header[data-testid="stHeader"], #MainMenu, footer, [data-testid="stToolbar"],
[data-testid="stDecoration"], [data-testid="stSidebar"], [data-testid="collapsedControl"] {
  display: none !important;
}
.block-container { max-width: 1120px; padding: 2.5rem 1.5rem 4rem; }

/* Header */
.vf-head { display: flex; align-items: flex-end; justify-content: space-between;
  gap: 1rem; flex-wrap: wrap; margin-bottom: 1.75rem; animation: vf-in .4s var(--vf-ease) both; }
.vf-brand { font-size: 2.5rem; font-weight: 600; letter-spacing: -0.025em; line-height: 1.05;
  color: var(--vf-text); margin: 0; }
.vf-sub { color: var(--vf-muted); font-size: 1.0625rem; margin: .35rem 0 0; }
.vf-pill { display: inline-flex; align-items: center; gap: .45rem; padding: .4rem .8rem;
  border-radius: 999px; background: var(--vf-card); box-shadow: var(--vf-shadow);
  font-size: .875rem; color: var(--vf-text); white-space: nowrap; }
.vf-dot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--vf-good); }
.vf-dot.stale { background: var(--vf-fair); }
.vf-pill.live .vf-dot { animation: vf-pulse 2.4s ease-in-out infinite; }
.vf-banner { background: #fff4e5; color: #8a4b00; border-radius: 14px; padding: .8rem 1rem;
  font-size: .9375rem; margin: -0.75rem 0 1.5rem; }

/* Widget labels: small caps, quiet */
[data-testid="stWidgetLabel"] p { font-size: .8125rem !important; font-weight: 600;
  color: var(--vf-faint); text-transform: uppercase; letter-spacing: .04em; }

/* Radios as segmented controls */
div[role="radiogroup"] { background: #e8e8ed; border-radius: 12px; padding: 3px;
  gap: 0 !important; display: inline-flex !important; flex-wrap: nowrap; width: auto; }
div[role="radiogroup"] > div { margin: 0 !important; }
[data-testid="stRadioOption"] { margin: 0 !important; padding: .42rem 1.1rem; border-radius: 9px;
  cursor: pointer; transition: background .2s var(--vf-ease), box-shadow .2s var(--vf-ease); }
[data-testid="stRadioOption"] > div > div:first-child { display: none !important; }
[data-testid="stRadioOption"] p { font-size: .9375rem; font-weight: 500; color: var(--vf-muted);
  white-space: nowrap; }
[data-testid="stRadioOption"][data-selected="true"] { background: var(--vf-card);
  box-shadow: 0 1px 3px rgba(0, 0, 0, .12); }
[data-testid="stRadioOption"][data-selected="true"] p { color: var(--vf-text); }

/* Tabs as a pill bar */
[role="tablist"] { gap: .25rem; background: #e8e8ed; padding: 4px; border-radius: 14px;
  border: 0 !important; overflow-x: auto; scrollbar-width: none; width: fit-content;
  max-width: 100%; margin-top: .5rem; }
[role="tablist"]::-webkit-scrollbar { display: none; }
[data-testid="stTab"] { padding: .5rem 1rem !important; border-radius: 10px; border: 0 !important;
  white-space: nowrap; transition: background .2s var(--vf-ease); }
[data-testid="stTab"] p { font-weight: 500; color: var(--vf-muted); }
[data-testid="stTab"][aria-selected="true"] { background: var(--vf-card);
  box-shadow: 0 1px 3px rgba(0, 0, 0, .12); }
[data-testid="stTab"][aria-selected="true"] p { color: var(--vf-text); }
.react-aria-SelectionIndicator { display: none !important; }
[role="tabpanel"] { padding-top: 1.5rem; }

/* Cards */
.vf-card { background: var(--vf-card); border-radius: var(--vf-radius);
  box-shadow: var(--vf-shadow);
  padding: 1.5rem; animation: vf-in .35s var(--vf-ease) both;
  transition: transform .25s var(--vf-ease), box-shadow .25s var(--vf-ease); }
.vf-grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
.vf-grid.tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.vf-grid .vf-card:hover { transform: translateY(-2px);
  box-shadow: 0 2px 4px rgba(0, 0, 0, .05), 0 14px 32px rgba(0, 0, 0, .08); }
.vf-eyebrow { font-size: .8125rem; font-weight: 600; color: var(--vf-faint);
  text-transform: uppercase; letter-spacing: .04em; }
.vf-big { font-size: 2.25rem; font-weight: 600; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums; color: var(--vf-text); line-height: 1.1; margin-top: .25rem; }
.vf-title { font-size: 1.375rem; font-weight: 600; letter-spacing: -0.015em; color: var(--vf-text);
  margin: 0; }
.vf-muted { color: var(--vf-muted); font-size: .9375rem; }

/* Verdict */
.vf-verdict { display: flex; gap: 1.1rem; align-items: center; }
.vf-badge { flex: none; width: 3rem; height: 3rem; border-radius: 50%; display: grid;
  place-items: center; color: #fff; font-size: 1.4rem; font-weight: 600; }

/* Rows with a probability bar */
.vf-list { display: flex; flex-direction: column; }
.vf-row { display: grid; grid-template-columns: 2rem 1fr minmax(90px, 30%) 3.6rem; gap: .9rem;
  align-items: center; padding: .85rem 0; border-top: 1px solid var(--vf-line); }
.vf-row:first-child { border-top: 0; }
.vf-rank { color: var(--vf-faint); font-variant-numeric: tabular-nums; font-size: .9375rem; }
.vf-row.dist { grid-template-columns: 3.75rem 1fr minmax(90px, 30%) 3.6rem; }
.vf-name { font-weight: 500; color: var(--vf-text); overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.vf-meta { color: var(--vf-muted); font-size: .8125rem; margin-top: .1rem; }
.vf-bar { height: 6px; border-radius: 3px; background: #e8e8ed; overflow: hidden; }
.vf-bar > span { display: block; height: 100%; border-radius: 3px;
  transition: width .6s var(--vf-ease); }
.vf-pct { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }

/* Forecast card lines */
.vf-line { display: flex; justify-content: space-between; align-items: baseline;
  padding: .55rem 0; border-top: 1px solid var(--vf-line); }
.vf-line:first-of-type { border-top: 0; }
.vf-val { font-variant-numeric: tabular-nums; font-weight: 600; }
.vf-chip { display: inline-block; margin-left: .4rem; padding: .1rem .45rem; border-radius: 6px;
  background: #f2f2f7; color: var(--vf-muted); font-size: .75rem; font-weight: 500; }
.vf-legend { display: flex; gap: 1.25rem; flex-wrap: wrap; color: var(--vf-muted);
  font-size: .875rem; margin-top: .9rem; }
.vf-legend i { display: inline-block; width: .65rem; height: .65rem; border-radius: 50%;
  margin-right: .4rem; vertical-align: middle; }
.vf-foot { color: var(--vf-faint); font-size: .8125rem; margin-top: 3rem; line-height: 1.6; }

/* Streamlit widgets in the same language */
[data-testid="stSelectbox"] > div > div, [data-baseweb="select"] > div {
  border-radius: 12px !important; }
[data-testid="stDeckGlJsonChart"] { border-radius: var(--vf-radius); overflow: hidden;
  box-shadow: var(--vf-shadow); }

@keyframes vf-in { from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; } }
@keyframes vf-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(52, 199, 89, .45); }
  50% { box-shadow: 0 0 0 5px rgba(52, 199, 89, 0); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after {
  animation: none !important; transition: none !important; } }
@media (max-width: 640px) {
  .block-container { padding: 1.25rem .9rem 3rem; }
  .vf-brand { font-size: 2rem; }
  .vf-card { padding: 1.1rem; border-radius: 16px; }
  .vf-big { font-size: 1.85rem; }
  .vf-row { grid-template-columns: 1.5rem 1fr 3.4rem; }
  .vf-row.dist { grid-template-columns: 3.25rem 1fr 3.4rem; }
  .vf-grid { gap: .6rem; }
  .vf-grid.tiles .vf-card { padding: .9rem; }
  .vf-grid.tiles .vf-big { font-size: 1.6rem; }
  .vf-grid.tiles .vf-eyebrow { font-size: .6875rem; }
  .vf-head { margin-bottom: 1rem; }
  [data-testid="stTab"] { padding: .45rem .7rem !important; }
  [data-testid="stTab"] span[role="img"] { display: none !important; }
  [data-testid="stTab"] p { font-size: .875rem; }
  [data-testid="stVerticalBlock"] { gap: .75rem; }
  .vf-row .vf-bar { display: none; }
}
</style>
"""


def percent(probability: float) -> str:
    """A probability for people: never "100 %" or "0 %", which no forecast is."""
    if probability >= 0.995:
        return "> 99%"
    if probability < 0.005:
        return "< 1%"
    return f"{probability:.0%}"


def tone(probability: float) -> str:
    """``good`` from 80 %, ``fair`` from 50 %, ``poor`` below."""
    if probability >= GOOD:
        return "good"
    return "fair" if probability >= FAIR else "poor"


def colour(probability: float) -> list[int]:
    """Map colour of a chance of success."""
    return {"good": GREEN, "fair": ORANGE, "poor": RED}[tone(probability)]


def risk_colour(probability: float) -> str:
    """Colour of a risk (empty or full): red from 50 %, orange from 20 %, grey below."""
    if probability >= FAIR:
        return TONES["poor"]
    return TONES["fair"] if probability >= 0.2 else NEUTRAL


def bar(probability: float, colour_hex: str | None = None) -> str:
    """A thin probability bar, coloured by tone unless a colour is given."""
    fill = colour_hex or TONES[tone(probability)]
    width = max(0.0, min(1.0, probability)) * 100
    return f'<div class="vf-bar"><span style="width:{width:.1f}%;background:{fill}"></span></div>'


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
                "station_id": s["station_id"],
                "lat": s["lat"],
                "lon": s["lon"],
                "name": s["name"],
                "chance": percent(chance),
                "now": f"{now['mechanical']:.0f} mechanical · {now['ebike']:.0f} e-bikes · "
                f"{now['docks']:.0f} docks",
                "colour": colour(chance),
            }
        )
    return points


def station_rows(station: dict[str, Any]) -> list[dict[str, Any]]:
    """Forecast table of a station: one row per horizon (the accessible view)."""
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


def central_index(names: list[str], by_name: dict[str, dict[str, Any]]) -> int:
    """Position in ``names`` of the station nearest to the centre of Paris."""
    return min(
        range(len(names)),
        key=lambda i: distance_m(*CENTRE, by_name[names[i]]["lat"], by_name[names[i]]["lon"]),
    )


def header_html(document: dict[str, Any], status: str, age_s: float) -> str:
    """Brand, subtitle and a live / delayed pill with the time of the data."""
    origin = datetime.fromisoformat(document["origin"]).astimezone(PARIS)
    stale = status == "stale"
    pill = (
        f'<span class="vf-pill {"" if stale else "live"}"><span class="vf-dot '
        f'{"stale" if stale else ""}"></span>{"Delayed" if stale else "Live"} · '
        f"{origin:%H:%M}</span>"
    )
    banner = (
        f'<div class="vf-banner">These forecasts are {age_s / 60:.0f} minutes old: '
        "the feed may be late.</div>"
        if stale
        else ""
    )
    return (
        f'<div class="vf-head"><div><h1 class="vf-brand">VéloFlow</h1>'
        f'<p class="vf-sub">Vélib\' bikes and docks, 15 to 60 minutes ahead · '
        f"{len(document['stations']):,} stations</p></div>{pill}</div>{banner}"
    )


def verdict_html(
    nearest: dict[str, Any] | None, best: dict[str, Any] | None, need: Need, horizon: str
) -> str:
    """The answer to "go now?", in one card."""
    thing = "a bike" if need == "bike" else "a dock"
    if nearest is not None and nearest["probability"] >= GOOD:
        choice, headline, mark, key = nearest, "Go now", "✓", "good"
    elif best is not None:
        choice, headline, mark, key = best, "Better nearby", "→", "fair"
    else:
        return (
            '<div class="vf-card vf-verdict"><div class="vf-badge" style="background:'
            f'{TONES["poor"]}">!</div><div><p class="vf-title">No safe bet within reach</p>'
            f'<p class="vf-muted">No station nearby has an 80% chance of {thing} in '
            f"{horizon} minutes. Widen the distance or try later.</p></div></div>"
        )
    name = escape(choice["station"]["name"])
    where = "you are here" if choice["distance_m"] < 30 else f"{choice['distance_m']:.0f} m away"
    return (
        f'<div class="vf-card vf-verdict"><div class="vf-badge" style="background:'
        f'{TONES[key]}">{mark}</div><div><div class="vf-eyebrow">{headline}</div>'
        f'<p class="vf-title">{name}</p><p class="vf-muted">{percent(choice["probability"])} '
        f"chance of {thing} in {horizon} min · {where}</p></div></div>"
    )


def options_html(options: list[dict[str, Any]]) -> str:
    """Nearby stations, nearest first, each with its chance of success."""
    if not options:
        return ""
    rows = "".join(
        f'<div class="vf-row dist"><span class="vf-rank">{o["distance_m"]:.0f} m</span>'
        f'<div><div class="vf-name">{escape(o["station"]["name"])}</div></div>'
        f'{bar(o["probability"])}<span class="vf-pct" style="color:'
        f'{TONES[tone(o["probability"])]}">{percent(o["probability"])}</span></div>'
        for o in options
    )
    return f'<div class="vf-card vf-list" style="margin-top:1rem">{rows}</div>'


def critical_html(ranked: list[tuple[float, dict[str, Any]]], event: str) -> str:
    """Stations most at risk, most likely first, with their state now."""
    if not ranked:
        return '<div class="vf-card vf-muted">No station is at risk right now.</div>'
    rows = "".join(
        f'<div class="vf-row"><span class="vf-rank">{i}</span><div><div class="vf-name">'
        f'{escape(s["name"])}</div><div class="vf-meta">{s["now"]["mechanical"]:.0f} mechanical · '
        f"{s['now']['ebike']:.0f} e-bikes · {s['now']['docks']:.0f} docks</div></div>"
        f"{bar(p, risk_colour(p))}"
        f'<span class="vf-pct">{percent(p)}</span></div>'
        for i, (p, s) in enumerate(ranked, start=1)
    )
    return f'<div class="vf-card vf-list" aria-label="Most likely {event}">{rows}</div>'


def now_html(station: dict[str, Any]) -> str:
    """Three tiles: what the station holds now."""
    now = station["now"]
    tiles = "".join(
        f'<div class="vf-card"><div class="vf-eyebrow">{label}</div>'
        f'<div class="vf-big">{now[key]:.0f}</div></div>'
        for label, key in (
            ("Mechanical", "mechanical"),
            ("E-bikes", "ebike"),
            ("Free docks", "docks"),
        )
    )
    return f'<div class="vf-grid tiles">{tiles}</div>'


def forecasts_html(station: dict[str, Any]) -> str:
    """One card per horizon: counts with their 80% interval, empty and full risks."""
    cards = []
    for horizon in HORIZONS:
        f = station["forecasts"].get(horizon)
        if f is None:
            continue
        lines = "".join(
            f'<div class="vf-line"><span class="vf-muted">{label}</span><span>'
            f'<span class="vf-val">{f[key]:.0f}</span><span class="vf-chip">'
            f"{f[key + '_interval'][0]:.0f}{DASH}{f[key + '_interval'][1]:.0f}</span></span></div>"
            for label, key in (
                ("Mechanical", "mechanical"),
                ("E-bikes", "ebike"),
                ("Docks", "docks"),
            )
        )
        risks = "".join(
            '<div style="margin-top:.8rem">'
            '<div class="vf-line" style="border:0;padding:0 0 .35rem">'
            f'<span class="vf-muted">{label}</span><span class="vf-val">{percent(f[key])}</span>'
            f"</div>{bar(f[key], risk_colour(f[key]))}</div>"
            for label, key in (("Empty", "p_empty"), ("Full", "p_full"))
        )
        cards.append(
            f'<div class="vf-card"><div class="vf-eyebrow">In {horizon} min</div>'
            f'<div style="margin-top:.6rem">{lines}</div>{risks}</div>'
        )
    return f'<div class="vf-grid" style="margin-top:1rem">{"".join(cards)}</div>'


def legend_html(need: Need) -> str:
    """Colour key of the map."""
    thing = "a bike" if need == "bike" else "a dock"
    return (
        f'<div class="vf-legend"><span><i style="background:#34c759"></i>80%+ chance of {thing}'
        f'</span><span><i style="background:#ff9f0a"></i>50{DASH}80%</span>'
        '<span><i style="background:#ff3b30"></i>under 50%</span>'
        "<span>Tap a station for its forecast</span></div>"
    )


def render() -> None:
    """Draw the app (called by Streamlit on every interaction)."""
    import pydeck as pdk
    import streamlit as st

    st.set_page_config(
        page_title="VéloFlow · Vélib' forecasts",
        page_icon="🚲",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.html(CSS)

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
    st.html(header_html(document, status, age))

    left, right, _ = st.columns([1, 1.4, 2])
    with left:
        need = NEEDS[st.radio("Looking for", list(NEEDS), horizontal=True)]
    with right:
        horizon = st.radio("In", HORIZONS, format_func=lambda h: f"{h} min", horizontal=True)
    by_name = {s["name"]: s for s in sorted(stations, key=lambda s: s["name"])}
    names = list(by_name)

    tab_map, tab_go, tab_critical, tab_station = st.tabs(
        [
            ":material/map: Map",
            ":material/near_me: Go now",
            ":material/warning: At risk",
            ":material/pin_drop: Station",
        ]
    )
    with tab_map:
        layer = pdk.Layer(
            "ScatterplotLayer",
            id="stations",
            data=map_points(stations, need, horizon),
            get_position="[lon, lat]",
            get_fill_color="colour",
            get_radius=40,
            radius_min_pixels=4,
            radius_max_pixels=11,
            stroked=True,
            get_line_color=[255, 255, 255, 230],
            line_width_min_pixels=1,
            pickable=True,
        )
        event_map = st.pydeck_chart(
            pdk.Deck(
                layers=[layer],
                initial_view_state=pdk.ViewState(latitude=48.86, longitude=2.34, zoom=11.6),
                map_style=pdk.map_styles.CARTO_LIGHT,
                tooltip={"text": "{name}\n{chance} chance\n{now}"},
            ),
            height=540,
            on_select="rerun",
            selection_mode="single-object",
        )
        st.html(legend_html(need))
        picked = (event_map.selection.objects.get("stations") or []) if event_map else []
        if picked:
            chosen = next((s for s in stations if s["station_id"] == picked[0]["station_id"]), None)
            if chosen is not None:
                st.html(
                    f'<p class="vf-title" style="margin-top:1.5rem">{escape(chosen["name"])}</p>'
                )
                st.html(now_html(chosen) + forecasts_html(chosen))
    with tab_go:
        cols = st.columns([2, 1])
        here = by_name[
            cols[0].selectbox("I am near", names, index=central_index(names, by_name), key="here")
        ]
        radius = cols[1].slider("Walking distance (m)", 200, 1500, 500, 100)
        options = options_near(stations, here["lat"], here["lon"], need, horizon, radius)
        st.html(
            verdict_html(options[0] if options else None, best_option(options, GOOD), need, horizon)
        )
        st.html(options_html(options))
    with tab_critical:
        event = NEED_EVENT[need]
        st.html(
            f'<p class="vf-muted" style="margin:0 0 1rem">The 20 stations most likely to be '
            f"<b>{event}</b> in {horizon} minutes.</p>"
        )
        st.html(critical_html(critical(stations, event, horizon, limit=20), event))
    with tab_station:
        station = by_name[
            st.selectbox("Station", names, index=central_index(names, by_name), key="station")
        ]
        st.html(now_html(station) + forecasts_html(station))
        st.html(
            '<p class="vf-muted" style="margin-top:1rem">Grey chips are 80% intervals. '
            "Probabilities are calibrated on past forecasts.</p>"
        )
        with st.expander("Table view"):
            st.dataframe(station_rows(station), hide_index=True, width="stretch")
    st.html(
        '<div class="vf-foot">Data: Vélib\' Métropole (ODbL) · weather: Open-Meteo (CC BY 4.0)'
        f"<br>Model {escape(document['model']['name'])}, trained on data up to "
        f"{escape(str(document['model']['data_end']))[:16].replace('T', ' ')} UTC</div>"
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
        "--browser.gatherUsageStats",
        "false",
        *[arg for key, value in THEME.items() for arg in (f"--theme.{key}", value)],
    ]
    sys.exit(cli.main())


if __name__ == "__main__":
    render()
