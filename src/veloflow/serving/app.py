"""VéloFlow web app (Streamlit): map, go now, at-risk stations, station page.

Reads the same forecast document as the API, through the same questions
(``serving.queries``). Run with ``uv run veloflow-app`` (port ``$PORT``,
8501 by default) and point ``VELOFLOW_FORECASTS`` at the document.

Requirements covered: F1 (map of every station with its forecast), F2 ("go
now?" with alternatives), F3 (stations most at risk), F4 (station page with
intervals and probabilities).

Layout: a dark sidebar with the navigation, a search bar and the freshness of
the data on top, then one page per task. Figures and cards come from
``serving.ui`` (pure functions); this module only arranges them.
"""

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veloflow.serving.api import DEFAULT_SOURCE, SOURCE_ENV, read_source
from veloflow.serving.queries import (
    NEED_EVENT,
    Need,
    best_option,
    critical,
    freshness,
    options_near,
)
from veloflow.serving.ui import (
    ACCENT,
    CENTRE,
    COUNT_COLOURS,
    DASH,
    GOOD,
    HORIZONS,
    brand_html,
    chance,
    count_cards_html,
    directions_url,
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
    risk_table_html,
    sidebar_status_html,
    smart_tip,
    station_head_html,
    station_trend,
    tip_html,
    topbar_html,
)

PAGES = {
    "Map": ":material/map:",
    "Go now": ":material/near_me:",
    "At risk": ":material/warning:",
    "Station": ":material/location_on:",
    "About": ":material/info:",
}
NEEDS: dict[str, Need] = {"Bike": "bike", "Dock": "dock"}
WALKS = {"5 min": 400, "10 min": 800, "15 min": 1200}

THEME = {
    "base": "light",
    "primaryColor": ACCENT,
    "backgroundColor": "#f5f5fa",
    "secondaryBackgroundColor": "#ffffff",
    "textColor": "#0f172a",
}

CSS = """
<style>
:root {
  --vf-bg: #f5f5fa; --vf-card: #ffffff; --vf-text: #0f172a; --vf-muted: #64748b;
  --vf-faint: #94a3b8; --vf-line: rgba(15, 23, 42, .07); --vf-accent: #6366f1;
  --vf-radius: 18px; --vf-ease: cubic-bezier(.2, .8, .2, 1);
  --vf-shadow: 0 1px 2px rgba(15, 23, 42, .04), 0 10px 30px rgba(15, 23, 42, .06);
}
html, body, .stApp, .stApp p, .stApp label, .stApp input, .stApp button, .stApp li {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif; -webkit-font-smoothing: antialiased; }
[data-testid="stIconMaterial"], span[translate="no"] {
  font-family: "Material Symbols Rounded" !important; }
.stApp { background: radial-gradient(1200px 500px at 85% -10%, #ece9ff 0%, transparent 60%),
  var(--vf-bg); }
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer, [data-testid="stDecoration"], [data-testid="stToolbarActions"],
[data-testid="stMainMenu"], [data-testid="stAppDeployButton"] { display: none !important; }
[data-testid="stExpandSidebarButton"] { background: #fff; border-radius: 12px;
  box-shadow: var(--vf-shadow); width: 2.5rem; height: 2.5rem; }
.block-container { max-width: 1440px; padding: 1.25rem 2rem 3rem; }

/* Sidebar */
[data-testid="stSidebar"] { background: linear-gradient(180deg, #0f1023 0%, #15143a 55%,
  #2a1f4d 80%, #6b3f6b 93%, #d9825b 100%); border-right: 0; }
[data-testid="stSidebar"] * { color: #e2e8f0; }
[data-testid="stSidebarHeader"] { height: .5rem; min-height: 0; }
.vf-brand { display: flex; flex-direction: column; align-items: center; gap: .35rem;
  padding: .5rem 0 1.75rem; }
.vf-brand-name { font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; color: #fff; }
.vf-brand-sub { font-size: .8rem; color: #cbd5e1; }
[data-testid="stSidebar"] div[role="radiogroup"] { display: flex; flex-direction: column;
  gap: .25rem; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] { padding: .7rem .9rem;
  border-radius: 12px;
  margin: 0 !important; transition: background .2s var(--vf-ease); }
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child { display:
  none !important; }
[data-testid="stSidebar"] [data-testid="stRadioOption"] p { font-size: 1rem; font-weight: 500;
  color: #cbd5e1 !important; }
[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover { background: rgba(255, 255,
  255, .06); }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
  background: linear-gradient(90deg, rgba(99, 102, 241, .55), rgba(139, 92, 246, .25));
  box-shadow: inset 3px 0 0 #a5b4fc; }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] p { color: #fff
  !important; }
.vf-side-live { display: flex; gap: .6rem; align-items: center; margin-top: 2rem;
  padding: .8rem 1rem;
  border-radius: 14px; background: rgba(15, 16, 35, .55);
  border: 1px solid rgba(255, 255, 255, .12); }
.vf-side-live b { display: block; color: #fff; font-size: .9rem; }
.vf-side-live small { color: #cbd5e1; font-size: .75rem; }

/* Top bar */
.vf-live { display: flex; gap: .6rem; align-items: center; justify-content: flex-end;
  padding-top: .35rem; }
.vf-live b { display: block; font-size: .875rem; color: var(--vf-text); }
.vf-live small { color: var(--vf-muted); font-size: .75rem; }
.vf-dot { width: .6rem; height: .6rem; border-radius: 50%; background: #22c55e; flex: none;
  animation: vf-pulse 2.4s ease-in-out infinite; }
.stale .vf-dot { background: #f59e0b; animation: none; }
[data-testid="stSelectbox"] [data-baseweb="select"] > div { border-radius: 14px !important;
  border: 1px solid var(--vf-line) !important; background: rgba(255, 255, 255, .85);
  min-height: 2.75rem; box-shadow: 0 1px 2px rgba(15, 23, 42, .03); }

/* Page heads */
.vf-page-head { margin: 1rem 0 1.25rem; animation: vf-in .35s var(--vf-ease) both; }
.vf-chip-accent { display: inline-flex; gap: .35rem; align-items: center;
  padding: .25rem .65rem;
  border-radius: 999px; background: #eef0ff; color: var(--vf-accent); font-size: .8rem;
  font-weight: 600; margin-bottom: .6rem; }
.vf-page-title { display: flex; gap: 1rem; align-items: center; }
.vf-page-icon { width: 3.5rem; height: 3.5rem; border-radius: 16px; background: #eef0ff;
  display: grid; place-items: center; flex: none; }
.vf-page-title h1 { font-size: 2.1rem; font-weight: 700; letter-spacing: -.025em; margin: 0;
  padding: 0; color: var(--vf-text); line-height: 1.15; }
.vf-page-title p { margin: .2rem 0 0; color: var(--vf-muted); font-size: 1.05rem; }

/* Cards and figures */
.vf-card { background: var(--vf-card); border-radius: var(--vf-radius);
  box-shadow: var(--vf-shadow);
  padding: 1.25rem; animation: vf-in .35s var(--vf-ease) both; }
.vf-kpis { display: grid; gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
.vf-kpi { display: flex; gap: 1rem; align-items: center; }
.vf-kpi-icon { width: 3.25rem; height: 3.25rem; border-radius: 50%; display: grid;
  place-items: center; flex: none; }
.vf-kpi-value { font-size: 1.75rem; font-weight: 700; letter-spacing: -.02em;
  color: var(--vf-text);
  font-variant-numeric: tabular-nums; line-height: 1.1; }
.vf-kpi-label { color: var(--vf-muted); font-size: .9rem; }
.vf-kpi-note { color: #16a34a; font-size: .8rem; margin-top: .15rem; }
.vf-section-title { display: flex; gap: .5rem; align-items: center; font-weight: 600;
  color: var(--vf-text); font-size: 1.05rem; margin: .25rem 0 .75rem; }
.vf-section-title small { color: var(--vf-faint); font-weight: 500; font-size: .8rem; }
.vf-station-head { display: flex; gap: .8rem; align-items: center; margin-bottom: .9rem; }
.vf-pin { width: 2.6rem; height: 2.6rem; border-radius: 12px; background: #eef0ff;
  display: grid;
  place-items: center; flex: none; }
.vf-station-name { font-size: 1.3rem; font-weight: 700; color: var(--vf-text);
  letter-spacing: -.015em; }
.vf-station-sub { color: var(--vf-muted); font-size: .875rem; }
.vf-counts { display: grid; gap: .75rem; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.vf-count { border-radius: 14px; padding: .9rem;
  background: color-mix(in srgb, var(--tint) 6%, white);
  border: 1px solid color-mix(in srgb, var(--tint) 14%, white); }
.vf-count-label { font-size: .85rem; color: var(--vf-text); font-weight: 600;
  margin-top: .4rem; }
.vf-count-value { font-size: 1.9rem; font-weight: 700; color: var(--vf-text); line-height: 1.15;
  font-variant-numeric: tabular-nums; }
.vf-count-note { color: var(--vf-muted); font-size: .78rem; margin-bottom: .55rem; }
.vf-bar { height: 6px; border-radius: 3px; background: #e2e8f0; overflow: hidden; }
.vf-bar > span { display: block; height: 100%; border-radius: 3px;
  transition: width .6s var(--vf-ease); }
.vf-risks { display: grid; gap: .75rem; grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: .75rem; }
.vf-risk { display: flex; gap: .8rem; align-items: center; padding: .9rem; border-radius: 14px;
  border: 1px solid var(--vf-line); background: #fbfbfe; }
.vf-ring { width: 1.6rem; height: 1.6rem; border-radius: 50%; border: 2.5px solid; flex: none; }
.vf-risk-body { flex: 1; }
.vf-risk-value { font-size: 1.35rem; font-weight: 700; color: var(--vf-text);
  line-height: 1.1; }
.vf-risk-label { color: var(--vf-muted); font-size: .82rem; margin-bottom: .4rem; }
.vf-hgrid { display: grid; gap: .6rem; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.vf-hcell { border: 1px solid var(--vf-line); border-radius: 12px; padding: .6rem;
  text-align: center;
  background: #fbfbfe; }
.vf-hcell-h { font-weight: 600; font-size: .85rem; }
.vf-hcell-v { color: var(--vf-muted); font-variant-numeric: tabular-nums; }
.vf-tip { display: flex; gap: .9rem; align-items: flex-start; background:
  linear-gradient(135deg, #fff7ed, #ffe4e6); box-shadow: none; }
.vf-tip.good { background: linear-gradient(135deg, #f0fdf4, #ecfeff); }
.vf-tip-icon { width: 2.4rem; height: 2.4rem; border-radius: 50%; background: #f59e0b;
  display: grid;
  place-items: center; flex: none; }
.vf-tip.good .vf-tip-icon { background: #22c55e; }
.vf-tip b { color: var(--vf-text); }
.vf-tip p { margin: .25rem 0 0; color: #475569; font-size: .92rem; }
.vf-netfig { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: center; height: 100%;
  padding: .5rem 0; }
.vf-netfig > div { display: flex; gap: .6rem; align-items: center; }
.vf-netfig b { display: block; font-size: 1.15rem; color: var(--vf-text); }
.vf-netfig small { color: var(--vf-muted); font-size: .8rem; }
.vf-table { width: 100%; border-collapse: collapse; font-size: .92rem; }
.vf-table th { text-align: left; color: var(--vf-faint); font-weight: 600; font-size: .78rem;
  padding: .4rem .5rem; border-bottom: 1px solid var(--vf-line); }
.vf-table td { padding: .5rem; border-bottom: 1px solid var(--vf-line); color: var(--vf-text); }
.vf-table tr:last-child td { border-bottom: 0; }
.vf-num { color: var(--vf-faint) !important; font-variant-numeric: tabular-nums; width: 2rem; }
.vf-muted { color: var(--vf-muted); }
.vf-badge { display: inline-block; padding: .15rem .55rem; border-radius: 8px; font-weight: 700;
  font-size: .85rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.vf-opts { display: flex; flex-direction: column; }
.vf-opt { display: flex; justify-content: space-between; align-items: center; gap: .75rem;
  padding: .6rem 0; border-top: 1px solid var(--vf-line); }
.vf-opt:first-child { border-top: 0; }
.vf-opt-name { font-weight: 500; color: var(--vf-text); }
.vf-opt small { color: var(--vf-muted); }
.vf-legend { display: flex; gap: 1.1rem; flex-wrap: wrap; color: var(--vf-muted);
  font-size: .85rem;
  margin-top: .6rem; }
.vf-legend i { display: inline-block; width: .65rem; height: .65rem; border-radius: 50%;
  margin-right: .4rem; vertical-align: middle; }
.vf-about { line-height: 1.65; color: #334155; }
.vf-icon { display: inline-block; vertical-align: middle; flex: none; }
.vf-minis { display: grid; gap: .6rem; grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-bottom: .9rem; }
.vf-mini { border-radius: 14px; padding: .75rem .5rem; text-align: center;
  background: color-mix(in srgb, var(--tint) 7%, white);
  border: 1px solid color-mix(in srgb, var(--tint) 14%, white); }
.vf-mini-value { font-size: 1.6rem; font-weight: 700; color: var(--vf-text); line-height: 1.2;
  font-variant-numeric: tabular-nums; }
.vf-mini-label { font-size: .78rem; color: var(--vf-muted); }
.vf-hcell-v { white-space: nowrap; font-size: .9rem; }
.vf-about h3 { margin-top: 1.25rem; }

/* Streamlit containers and widgets in the same language */
[data-testid="stDeckGlJsonChart"] { border-radius: var(--vf-radius); overflow: hidden;
  box-shadow: var(--vf-shadow); }
[data-testid="stMain"] div[role="radiogroup"] { background: #eceef5; border-radius: 12px;
  padding: 3px;
  display: inline-flex !important; gap: 0 !important; flex-wrap: nowrap; }
[data-testid="stMain"] div[role="radiogroup"] > div { margin: 0 !important; }
[data-testid="stMain"] [data-testid="stRadioOption"] { margin: 0 !important;
  padding: .38rem .95rem; border-radius: 9px; }
[data-testid="stMain"] [data-testid="stRadioOption"] > div > div:first-child { display: none
  !important; }
[data-testid="stMain"] [data-testid="stRadioOption"] p { font-size: .9rem; font-weight: 500;
  color: var(--vf-muted);
  white-space: nowrap; }
[data-testid="stMain"] [data-testid="stRadioOption"][data-selected="true"] {
  background: linear-gradient(135deg, #6366f1, #8b5cf6);
  box-shadow: 0 2px 8px rgba(99, 102, 241, .35); }
[data-testid="stMain"] [data-testid="stRadioOption"][data-selected="true"] p { color: #fff; }
[data-testid="stWidgetLabel"] p { font-size: .8rem !important; font-weight: 600;
  color: var(--vf-faint); }
[data-testid="stLinkButton"] a { background: #0f172a; color: #fff !important;
  border-radius: 12px;
  border: 0; width: 100%; padding: .7rem; font-weight: 600; }
[data-testid="stLinkButton"] a:hover { background: #1e293b; }
[data-testid="stVegaLiteChart"] { background: transparent; }

@keyframes vf-in { from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; } }
@keyframes vf-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(34, 197, 94, .45); }
  50% { box-shadow: 0 0 0 5px rgba(34, 197, 94, 0); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after {
  animation: none !important; transition: none !important; } }
@media (max-width: 760px) {
  .block-container { padding: 3.75rem .9rem 2.5rem; }
  .vf-page-title h1 { font-size: 1.6rem; }
  .vf-page-icon { width: 2.8rem; height: 2.8rem; }
  .vf-kpi-value { font-size: 1.4rem; }
  .vf-counts { gap: .5rem; }
  .vf-count { padding: .7rem; }
  .vf-count-value { font-size: 1.5rem; }
  .vf-live { justify-content: flex-start; }
}
</style>
"""


def _deck(
    points: list[dict[str, Any]],
    *,
    centre: tuple[float, float],
    zoom: float,
    highlight: dict[str, Any] | None = None,
    origin: dict[str, Any] | None = None,
    faded: bool = False,
) -> Any:
    """A pydeck map of stations, optionally a highlighted station and a walk line."""
    import pydeck as pdk

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            id="stations",
            data=points,
            get_position="[lon, lat]",
            get_fill_color="colour",
            get_radius=38,
            radius_min_pixels=3 if faded else 4,
            radius_max_pixels=10,
            opacity=0.55 if faded else 1.0,
            stroked=True,
            get_line_color=[255, 255, 255, 220],
            line_width_min_pixels=1,
            pickable=True,
        )
    ]
    if origin is not None and highlight is not None:
        layers.append(
            pdk.Layer(
                "LineLayer",
                id="walk",
                data=[
                    {
                        "from": [origin["lon"], origin["lat"]],
                        "to": [highlight["lon"], highlight["lat"]],
                    }
                ],
                get_source_position="from",
                get_target_position="to",
                get_color=[99, 102, 241, 220],
                get_width=4,
            )
        )
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="you",
                data=[origin],
                get_position="[lon, lat]",
                get_fill_color=[37, 99, 235, 255],
                get_radius=60,
                radius_min_pixels=8,
                stroked=True,
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=3,
            )
        )
    if highlight is not None:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="selected",
                data=[highlight],
                get_position="[lon, lat]",
                get_fill_color=[99, 102, 241, 255],
                get_radius=70,
                radius_min_pixels=9,
                stroked=True,
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=3,
            )
        )
        layers.append(
            pdk.Layer(
                "TextLayer",
                id="label",
                data=[highlight],
                get_position="[lon, lat]",
                get_text="name",
                get_size=14,
                get_color=[15, 23, 42, 255],
                get_pixel_offset=[0, -22],
                background=True,
                get_background_color=[255, 255, 255, 235],
                background_padding=[8, 4],
                font_weight=600,
            )
        )
    return pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=centre[0], longitude=centre[1], zoom=zoom),
        map_style=pdk.map_styles.CARTO_LIGHT,
        tooltip={"text": "{name}\n{chance} chance\n{now}"},
    )


def _trend_chart(points: list[tuple[str, float]], *, share: bool, colour: str) -> Any:
    """A small area chart over Now, 15, 30 and 60 minutes."""
    import altair as alt

    data = alt.Data(  # type: ignore[no-untyped-call]
        values=[{"when": w, "value": v, "order": i} for i, (w, v) in enumerate(points)]
    )
    order = [w for w, _ in points]
    axis_y = alt.Axis(
        format="%" if share else "d",
        grid=True,
        gridColor="#eef0f5",
        domain=False,
        ticks=False,
        labelColor="#94a3b8",
        title=None,
        tickCount=3,
    )
    base = alt.Chart(data).encode(
        x=alt.X(
            "when:N",
            sort=order,
            axis=alt.Axis(
                labelAngle=0, title=None, domain=False, ticks=False, labelColor="#64748b"
            ),
        ),
        y=alt.Y(
            "value:Q",
            axis=axis_y,
            scale=alt.Scale(domain=[0, 1]) if share else alt.Scale(zero=True),
        ),
    )
    area = base.mark_area(
        interpolate="monotone",
        line={"color": colour, "strokeWidth": 2.5},
        color=alt.Gradient(  # type: ignore[no-untyped-call]
            gradient="linear",
            stops=[
                alt.GradientStop(color=f"{colour}55", offset=0),
                alt.GradientStop(color=f"{colour}05", offset=1),
            ],
            x1=1,
            x2=1,
            y1=0,
            y2=1,
        ),
    )
    dots = base.mark_circle(size=60, color=colour).encode(
        tooltip=[
            alt.Tooltip("when:N", title="When"),
            alt.Tooltip("value:Q", title="Value", format=".0%" if share else ".1f"),
        ]
    )
    return (area + dots).properties(height=190).configure_view(strokeWidth=0)


def render() -> None:
    """Draw the app (called by Streamlit on every interaction)."""
    import streamlit as st

    st.set_page_config(
        page_title="VéloFlow · Vélib' forecasts",
        page_icon="🚲",
        layout="wide",
        initial_sidebar_state="auto",
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
    by_name = {s["name"]: s for s in sorted(stations, key=lambda s: s["name"])}
    names = list(by_name)
    by_id = {s["station_id"]: s for s in stations}
    state = st.session_state
    state.setdefault("station", names[nearest_index(names, by_name, *CENTRE)] if names else None)
    state.setdefault("page", "Map")

    def open_station() -> None:
        if state.get("search"):
            state["station"] = state["search"]
            state["page"] = "Station"
            state["search"] = None

    # Sidebar: brand, navigation, freshness.
    with st.sidebar:
        st.html(brand_html())
        st.radio(
            "Navigation",
            list(PAGES),
            key="page",
            label_visibility="collapsed",
            format_func=lambda p: f"{PAGES[p]}  {p}",
        )
        st.html(sidebar_status_html(document, status))

    # Top bar: station search and freshness.
    search, live = st.columns([4, 1], vertical_alignment="center")
    search.selectbox(
        "Search a station",
        names,
        index=None,
        key="search",
        on_change=open_station,
        placeholder="Search a station (e.g. Bastille, République...)",
        label_visibility="collapsed",
    )
    live.html(topbar_html(document, status, age))
    if status == "stale":
        st.warning(f"These forecasts are {age / 60:.0f} minutes old: the feed may be late.")

    page = state["page"]
    summary = network_summary(stations)

    if page == "Map":
        st.html(
            page_head_html(
                "map",
                "Vélib' station forecasts",
                "Real-time and predictive availability across Paris and its suburbs",
                eyebrow="Map",
            )
        )
        controls = st.columns([1, 1.2, 3])
        need = NEEDS[controls[0].radio("Looking for", list(NEEDS), horizontal=True, key="map_need")]
        horizon = controls[1].radio(
            "In", HORIZONS, horizontal=True, key="map_h", format_func=lambda h: f"{h} min"
        )
        st.html(
            '<div class="vf-kpis">'
            + kpi_html("bike", "#4f6bed", f"{summary['stations']:,.0f}", "Stations forecast")
            + kpi_html("clock", "#8b5cf6", f"{horizon} min", "Forecast horizon")
            + kpi_html("pie", "#f97316", percent(summary["with_bike"]), "Stations with a bike now")
            + "</div>"
        )
        left, right = st.columns([1.5, 1], gap="medium")
        selected = by_name.get(state["station"])
        with left:
            event = st.pydeck_chart(
                _deck(
                    map_points(stations, need, horizon),
                    centre=(48.858, 2.345),
                    zoom=11.6,
                    highlight=selected,
                ),
                height=520,
                on_select="rerun",
                selection_mode="single-object",
            )
            label = "bike" if need == "bike" else "dock"
            st.html(legend_html((f"80%+ chance of a {label}", f"50{DASH}80%", "Under 50%")))
            picked = (event.selection.objects.get("stations") or []) if event else []
            if picked and picked[0]["station_id"] in by_id:
                chosen = by_id[picked[0]["station_id"]]["name"]
                if chosen != state["station"]:
                    state["station"] = chosen
                    st.rerun()
        with right, st.container(border=False):
            if selected is not None:
                st.html(
                    '<div class="vf-card">'
                    + station_head_html(selected)
                    + f'<div class="vf-section-title">Forecast in {horizon} min</div>'
                    + count_cards_html(selected, horizon)
                    + risk_cards_html(selected, horizon)
                    + "</div>"
                )
                if st.button("Open station page", width="stretch"):
                    state["page"] = "Station"
                    st.rerun()
        st.html('<div style="height:1rem"></div>')
        chart, figures, tip = st.columns([1.5, 1.2, 1], gap="medium", vertical_alignment="center")
        with chart:
            st.html(
                f'<div class="vf-section-title">{icon("chart", 18)} Network overview '
                "<small>share of stations with a bike</small></div>"
            )
            st.altair_chart(
                _trend_chart(network_trend(stations), share=True, colour="#6366f1"), width="stretch"
            )
        figures.html(network_figures_html(summary))
        tip.html(tip_html("Smart tip", smart_tip(stations, need, horizon)))

    elif page == "Go now":
        st.html(
            page_head_html("nav", "Go now", "Find the best Vélib' station near you, right now.")
        )
        panel, middle, card = st.columns([1, 1.5, 1.1], gap="medium")
        with panel:
            here = by_name[
                st.selectbox(
                    "Your location (nearest station)",
                    names,
                    index=names.index(state["station"]),
                    key="here",
                )
            ]
            need = NEEDS[
                st.radio(
                    "Find me a station with",
                    list(NEEDS),
                    horizontal=True,
                    format_func=lambda n: f"a {n.lower()}",
                    key="go_need",
                )
            ]
            walk = st.radio("Walking time", list(WALKS), horizontal=True, key="walk")
            options = options_near(stations, here["lat"], here["lon"], need, "15", WALKS[walk])
            st.html(options_html(options, need))
            st.html(
                tip_html(
                    "Tip",
                    "Green stations are the safest bets. The chance is for 15 "
                    "minutes from now: about the time to walk there.",
                )
            )
        best = best_option(options, GOOD) or (options[0] if options else None)
        with middle:
            st.pydeck_chart(
                _deck(
                    map_points(stations, need, "15"),
                    centre=(here["lat"], here["lon"]),
                    zoom=14.2,
                    highlight=best["station"] if best else None,
                    origin=here,
                ),
                height=560,
            )
            st.html(legend_html(("80%+ chance", f"50{DASH}80%", "Under 50%")))
        with card:
            if best is None:
                st.html(tip_html("No station in reach", "Widen the walking time.", "fair"))
            else:
                station = best["station"]
                minutes = max(1, round(best["distance_m"] / 80))
                st.html(
                    '<div class="vf-card">'
                    + station_head_html(
                        station, f" · {best['distance_m']:.0f} m · ~{minutes} min walk"
                    )
                    + f'<div class="vf-section-title">{percent(chance(station, need, "15"))} '
                    f"chance of a {'bike' if need == 'bike' else 'dock'} in 15 min</div>"
                    + now_compact_html(station)
                    + '<div style="height:.75rem"></div>'
                    + horizon_table_html(station)
                    + risk_cards_html(station, "15")
                    + "</div>"
                )
                st.link_button(
                    "Get directions",
                    directions_url(station["lat"], station["lon"]),
                    icon=":material/near_me:",
                    width="stretch",
                )

    elif page == "At risk":
        head = st.empty()  # the title depends on the controls below it
        controls = st.columns([1, 1.2, 3])
        need = NEEDS[
            controls[0].radio("Looking for", list(NEEDS), horizontal=True, key="risk_need")
        ]
        horizon = controls[1].radio(
            "In", HORIZONS, horizontal=True, key="risk_h", format_func=lambda h: f"{h} min"
        )
        event_name = NEED_EVENT[need]
        ranked = critical(stations, event_name, horizon, limit=20)
        head.html(
            page_head_html(
                "alert",
                "At risk",
                f"The 20 stations most likely to be {event_name} in {horizon} minutes.",
            )
        )
        average = sum(p for p, _ in ranked) / len(ranked) if ranked else 0.0
        st.html(
            '<div class="vf-kpis">'
            + kpi_html("alert", "#ef4444", str(len(ranked)), "At-risk stations listed")
            + kpi_html("clock", "#8b5cf6", f"{horizon} min", "Forecast horizon")
            + kpi_html("pie", "#a855f7", percent(average), f"Average P({event_name})")
            + "</div>"
        )
        table, map_column = st.columns([1, 1.35], gap="medium")
        table.html(risk_table_html(ranked, horizon, event_name))
        risky = {s["station_id"] for _, s in ranked}
        points = map_points(stations, need, horizon)
        for point in points:
            if point["station_id"] in risky:
                point["colour"] = [239, 68, 68, 255]
            else:
                point["colour"] = [148, 163, 184, 110]
        with map_column:
            st.pydeck_chart(_deck(points, centre=(48.858, 2.345), zoom=11.8), height=620)
            st.html(
                '<div class="vf-legend"><span><i style="background:#ef4444"></i>At risk</span>'
                '<span><i style="background:#94a3b8"></i>Other stations</span></div>'
            )

    elif page == "Station":
        station = by_name[state["station"]]
        head, pick = st.columns([2, 1], vertical_alignment="bottom")
        head.html(station_head_html(station).replace("vf-station-name", "vf-station-name vf-xl"))
        pick.selectbox(
            "Station",
            names,
            index=names.index(state["station"]),
            key="station_pick",
            on_change=lambda: state.update(station=state["station_pick"]),
        )
        st.html(now_tiles_html(station))
        st.html('<div style="height:.75rem"></div>')
        map_column, forecast = st.columns([1.3, 1], gap="medium")
        with map_column:
            st.pydeck_chart(
                _deck(
                    map_points(stations, "bike", "15"),
                    centre=(station["lat"], station["lon"]),
                    zoom=14.6,
                    highlight=station,
                    faded=True,
                ),
                height=430,
            )
        with forecast:
            horizon = st.radio(
                "Forecast",
                HORIZONS,
                horizontal=True,
                key="station_h",
                format_func=lambda h: f"{h} min",
            )
            st.html(
                '<div class="vf-card">'
                + count_cards_html(station, horizon)
                + risk_cards_html(station, horizon)
                + "</div>"
            )
        trend, insights = st.columns([1.5, 1], gap="medium", vertical_alignment="center")
        with trend:
            kind = st.radio(
                "Availability trend",
                ["mechanical", "ebike", "docks"],
                horizontal=True,
                key="trend",
                format_func={"mechanical": "Mechanical", "ebike": "E-bikes", "docks": "Docks"}.get,
            )
            st.altair_chart(
                _trend_chart(station_trend(station, kind), share=False, colour=COUNT_COLOURS[kind]),
                width="stretch",
            )
        text, mood = insight(station, horizon)
        insights.html(tip_html("Insight", text, mood))

    else:
        st.html(
            page_head_html(
                "info", "About VéloFlow", "How the forecasts are made, and what they are worth."
            )
        )
        st.html(
            '<div class="vf-card vf-about"><p>VéloFlow forecasts, for every Vélib\' Métropole '
            "station, the number of mechanical bikes, e-bikes and free docks 15, 30 and 60 minutes "
            "ahead, and the probability that the station will be empty or full.</p>"
            "<h3>Data</h3><p>The public Vélib' feed, collected every 30 seconds since 8 November "
            "2026 and cleaned by explicit quality rules (stale versions, closed stations, frozen "
            "feed); Open-Meteo weather forecasts; the French school calendar.</p>"
            "<h3>Model</h3><p>A global LightGBM model per horizon, trained on every station at "
            "once: it forecasts the change from now, and calibrated probabilities of empty and "
            "full. Every model is evaluated by the same backtest, which fails if a forecast reads "
            "data from after its own time.</p>"
            '<h3>Honesty</h3><p>With a short history, the model matches the simple "nothing '
            'changes" forecast on counts, and is clearly better at ranking the stations about to '
            "run empty. The numbers are re-measured as the history grows.</p>"
            '<p><a href="https://github.com/HamzaOuba00-1/veloflow">Source code and evaluation '
            "on GitHub</a></p></div>"
        )

    st.html(
        '<p class="vf-muted" style="font-size:.8rem;margin-top:2.5rem">Data: Vélib\' Métropole '
        "(ODbL) · weather: Open-Meteo (CC BY 4.0) · model "
        f"{document['model']['name']}, trained on data up to "
        f"{str(document['model']['data_end'])[:16].replace('T', ' ')} UTC</p>"
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
