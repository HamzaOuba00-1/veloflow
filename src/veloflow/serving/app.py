"""VéloFlow web app (Streamlit): landing page, map, go now, at-risk stations, station page.

Reads the same forecast document as the API, through the same questions
(``serving.queries``). Run with ``uv run veloflow-app`` (port ``$PORT``,
8501 by default) and point ``VELOFLOW_FORECASTS`` at the document.

Requirements covered: F1 (map of every station with its forecast), F2 ("go
now?" with alternatives), F3 (stations most at risk), F4 (station page with
intervals and probabilities), F6 (service health, Admin page).

Layout: a sticky header (brand, navigation, freshness of the data), then one
page per task, then the footer. The page and the station live in the URL
(``?page=station&station=6245``), so any view can be shared or bookmarked.
Look and feel come from the design system (``serving.theme`` and
``static/css/veloflow.css``); components from ``serving.ui``,
``serving.landing`` and ``serving.admin`` (pure functions). This module only
arranges them.
"""

import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veloflow.serving import theme
from veloflow.serving.admin import (
    accuracy_html,
    alerts_html,
    events_html,
    health_html,
    models_html,
)
from veloflow.serving.landing import (
    FEATURES,
    cta_background_html,
    cta_html,
    demo_card_html,
    feature_html,
    footer_html,
    hero_text_html,
    hero_visual_html,
    network_map_uri,
    section_head_html,
    steps_html,
    story_html,
)
from veloflow.serving.queries import (
    NEED_EVENT,
    Need,
    best_option,
    critical,
    freshness,
    options_near,
)
from veloflow.serving.source import (
    DEFAULT_SOURCE,
    SOURCE_ENV,
    ForecastStore,
    ForecastsUnavailableError,
)
from veloflow.serving.ui import (
    ACCENT,
    CENTRE,
    COUNT_COLOURS,
    FAVICON,
    GITHUB,
    GOOD,
    RED,
    WALK_M_PER_MIN,
    about_html,
    band_labels,
    brand_html,
    chance,
    count_cards_html,
    directions_url,
    empty_html,
    hex_colour,
    horizon_table_html,
    horizons,
    insight,
    legend_html,
    live_status_html,
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
    smart_tip,
    stat_html,
    station_head_html,
    station_trend,
    stats_html,
    tip_html,
    walk_minutes,
)

# Page label -> slug in the URL (``?page=<slug>``).
PAGES = {
    "Home": "home",
    "Map": "map",
    "Go now": "go",
    "At risk": "at-risk",
    "Station": "station",
    "Admin": "admin",
    "About": "about",
}
SLUGS = {slug: label for label, slug in PAGES.items()}
# Icons of the navigation: shown in the phone's bottom tab bar, hidden on desktop.
NAV_ICONS = {
    "Home": ":material/home:",
    "Map": ":material/map:",
    "Go now": ":material/near_me:",
    "At risk": ":material/warning:",
    "Station": ":material/location_on:",
    "Admin": ":material/monitoring:",
    "About": ":material/info:",
}
# On phones the tab bar holds the first five pages; these open from the "More" menu.
SECONDARY_PAGES = ("Admin", "About")
NEEDS: dict[str, Need] = {"Bike": "bike", "Dock": "dock"}
WALK_LIMITS_MIN = (5, 10, 15)  # walking-time choices; distances follow from WALK_M_PER_MIN
TOP_AT_RISK = 20  # stations listed on the At risk page
PARIS_VIEW = (48.858, 2.345)  # centre of the network maps
LABEL_FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"  # system: drawn at once
LABEL_CHARACTERS = "".join(chr(c) for c in range(32, 127))  # map labels: ASCII, then the name

# Streamlit's own theme, aligned with the design tokens (widgets it draws itself).
THEME = {
    "base": "light",
    "primaryColor": theme.PRIMARY,
    "backgroundColor": theme.BACKGROUND,
    "secondaryBackgroundColor": theme.SURFACE_MUTED,
    "textColor": theme.TEXT,
    "font": "sans-serif",
}


def page_now(state: Any) -> str:
    """The page being shown (set by the navigation, a button or the URL)."""
    return str(state.get("page", "Home"))


def _deck(
    points: list[dict[str, Any]],
    *,
    centre: tuple[float, float],
    zoom: float,
    highlight: dict[str, Any] | None = None,
    origin: dict[str, Any] | None = None,
    faded: bool = False,
    emphasis: list[dict[str, Any]] | None = None,
) -> Any:
    """A pydeck map of stations, optionally a highlighted station and a walk line.

    ``emphasis``: points drawn larger, on top of the others (stations at risk).
    """
    import pydeck as pdk

    layers = [
        pdk.Layer(
            "ScatterplotLayer",
            id="stations",
            data=points,
            get_position="[lon, lat]",
            get_fill_color="colour",
            get_radius=36,
            radius_min_pixels=3 if faded else 3.5,
            radius_max_pixels=9,
            opacity=0.5 if faded else 1.0,
            stroked=True,
            get_line_color=[255, 255, 255, 230],
            line_width_min_pixels=1,
            pickable=True,
            auto_highlight=True,
            highlight_color=[*theme.MAP_PRIMARY[:3], 120],
        )
    ]
    if emphasis:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="emphasis",
                data=emphasis,
                get_position="[lon, lat]",
                get_fill_color="colour",
                get_radius=70,
                radius_min_pixels=6,
                radius_max_pixels=14,
                stroked=True,
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=2,
                pickable=True,
            )
        )
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
                get_color=[*theme.MAP_PRIMARY[:3], 200],
                get_width=3,
                width_min_pixels=3,
            )
        )
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="you",
                data=[origin],
                get_position="[lon, lat]",
                get_fill_color=theme.MAP_HERE,
                get_radius=50,
                radius_min_pixels=7,
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
                get_fill_color=theme.MAP_PRIMARY,
                get_radius=60,
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
                get_size=13,
                get_color=[28, 26, 39, 255],
                get_pixel_offset=[0, -30],
                font_family=pdk.types.String(LABEL_FONT),
                font_weight=600,
                # Accented names (Châtelet, Opéra): their letters join the font
                # atlas. A literal string: pydeck reads plain strings as code.
                character_set=pdk.types.String(LABEL_CHARACTERS + highlight["name"]),
                background=True,
                get_background_color=[255, 255, 255, 240],
                background_padding=[8, 5],
            )
        )
    return pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=centre[0], longitude=centre[1], zoom=zoom),
        map_style=pdk.map_styles.CARTO_LIGHT,
        tooltip={
            "html": "<b>{name}</b><br/>{chance} chance<br/><span>{now}</span>",
            "style": {
                "backgroundColor": theme.SECONDARY,
                "color": "#ffffff",
                "fontFamily": "Inter, -apple-system, 'Segoe UI', sans-serif",
                "fontSize": "13px",
                "borderRadius": "10px",
                "padding": "8px 12px",
            },
        },
    )


def _trend_chart(points: list[tuple[str, float]], *, share: bool, colour: str) -> Any:
    """A small area chart over Now and each horizon."""
    import altair as alt

    data = alt.Data(  # type: ignore[no-untyped-call]
        values=[{"when": w, "value": v, "order": i} for i, (w, v) in enumerate(points)]
    )
    order = [w for w, _ in points]
    axis_y = alt.Axis(
        format="%" if share else "d",
        grid=True,
        gridColor=theme.BORDER,
        domain=False,
        ticks=False,
        labelColor=theme.TEXT_TERTIARY,
        labelPadding=8,
        title=None,
        tickCount=3,
    )
    base = alt.Chart(data).encode(
        x=alt.X(
            "when:N",
            sort=order,
            scale=alt.Scale(paddingOuter=0.05),
            axis=alt.Axis(
                labelAngle=0,
                title=None,
                domain=False,
                ticks=False,
                labelColor=theme.TEXT_SECONDARY,
                labelPadding=10,
            ),
        ),
        y=alt.Y(
            "value:Q",
            axis=axis_y,
            scale=alt.Scale(domain=[0, 1]) if share else alt.Scale(zero=True, nice=True),
        ),
    )
    area = base.mark_area(
        interpolate="monotone",
        line={"color": colour, "strokeWidth": 2.5},
        color=alt.Gradient(  # type: ignore[no-untyped-call]
            gradient="linear",
            stops=[
                alt.GradientStop(color=f"{colour}40", offset=0),
                alt.GradientStop(color=f"{colour}00", offset=1),
            ],
            x1=1,
            x2=1,
            y1=0,
            y2=1,
        ),
    )
    dots = base.mark_circle(size=70, color=colour, stroke="#ffffff", strokeWidth=2).encode(
        tooltip=[
            alt.Tooltip("when:N", title="When"),
            alt.Tooltip("value:Q", title="Value", format=".0%" if share else ".1f"),
        ]
    )
    return (
        (area + dots)
        .properties(height=200)
        .configure(font="Inter, -apple-system, 'Segoe UI', sans-serif", background="transparent")
        .configure_axis(labelFontSize=12)
        .configure_view(strokeWidth=0)
    )


def render() -> None:
    """Draw the app (called by Streamlit on every interaction)."""
    import streamlit as st

    st.set_page_config(
        page_title="VéloFlow · Vélib' forecasts",
        page_icon=str(FAVICON),
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.html(theme.style_sheet())

    @st.cache_resource(show_spinner=False)
    def store(source: str) -> ForecastStore:
        # One store per server, shared by every visitor: the document is
        # read at most once a minute and the last good one is kept.
        return ForecastStore(source)

    try:
        with st.spinner("Loading the latest forecasts…"):
            document = store(os.environ.get(SOURCE_ENV, DEFAULT_SOURCE)).document()
    except ForecastsUnavailableError as error:
        st.html(f'<div style="padding:var(--vf-space-6) 0">{brand_html()}</div>')
        st.html(
            page_head_html(
                "Forecasts are on their way",
                "The latest forecast update could not be read. The app shows it again "
                "as soon as a valid one arrives.",
            )
        )
        st.error(f"Forecasts are unavailable right now ({error}).")
        return

    stations: list[dict[str, Any]] = document["stations"]
    status, age = freshness(document, datetime.now(UTC))
    by_name = {s["name"]: s for s in sorted(stations, key=lambda s: s["name"])}
    names = list(by_name)
    by_id = {s["station_id"]: s for s in stations}
    state = st.session_state

    # First run of a session: the page and the station come from the URL.
    if "page" not in state:
        state["page"] = SLUGS.get(str(st.query_params.get("page", "")), "Home")
        wanted = str(st.query_params.get("station", ""))
        if wanted.isdigit() and int(wanted) in by_id:
            state["station"] = by_id[int(wanted)]["name"]
    # The checked document has stations; the one chosen earlier may have left
    # the feed since (closed, removed): back to the station nearest the centre.
    if state.get("station") not in by_name:
        state["station"] = names[nearest_index(names, by_name, *CENTRE)]
    for key in ("here", "station_pick"):  # station pickers remember their choice
        if key in state and state[key] not in by_name:
            del state[key]
    if state["page"] not in PAGES:
        state["page"] = "Home"

    def go(page: str) -> Callable[[], None]:
        # A callback: it runs before the navigation radio (key "page") is
        # drawn, the only moment Streamlit lets the page change. It also
        # closes the phone's "More" menu, which would otherwise stay open.
        def change() -> None:
            state["page"] = page
            if state.get("more"):
                state["more"] = False

        return change

    # Header: brand, navigation, freshness. On phones (style sheet) the
    # navigation becomes a tab bar at the bottom of the screen and the pages it
    # leaves out open from the "More" menu beside the freshness pill.
    with st.container(key="vf-header"):
        brand, nav, live = st.columns([1, 4, 1], vertical_alignment="center", gap="small")
        brand.button("VéloFlow", key="brand", on_click=go("Home"))
        nav.radio(
            "Navigation",
            list(PAGES),
            key="page",
            horizontal=True,
            label_visibility="collapsed",
            format_func=lambda p: f"{NAV_ICONS[p]} {p}",
        )
        with live, st.container(horizontal=True, key="header-end", gap="small"):
            st.html(live_status_html(document, status, age))
            with st.popover(
                "More",
                icon=":material/more_horiz:",
                key="more",
                # tinted while one of its pages is shown
                type="primary" if page_now(state) in SECONDARY_PAGES else "secondary",
                on_change="rerun",  # its open state lives in state["more"]: go() closes it
            ):
                for label in SECONDARY_PAGES:
                    st.button(
                        label,
                        icon=NAV_ICONS[label],
                        key=f"more-{PAGES[label]}",
                        on_click=go(label),
                        type="primary" if page_now(state) == label else "secondary",
                        width="stretch",
                    )
                st.link_button("Source code", GITHUB, icon=":material/code:", width="stretch")

    page = state["page"]
    query = {"page": PAGES[page]}
    if page == "Station":
        query["station"] = str(by_name[state["station"]]["station_id"])
    if st.query_params.to_dict() != query:
        st.query_params.from_dict(query)

    summary = network_summary(stations)
    steps = horizons(stations)  # forecast horizons of this document, shortest first
    pages: dict[str, Callable[[], None]] = {
        "Home": lambda: _home(st, document, stations, by_name, names, steps, status, go),
        "Map": lambda: _map(st, stations, by_name, by_id, steps, summary, go),
        "Go now": lambda: _go_now(st, stations, by_name, names, steps),
        "At risk": lambda: _at_risk(st, stations, steps),
        "Station": lambda: _station(st, stations, by_name, names),
        "Admin": lambda: _admin(st, document),
        "About": lambda: _about(st, document),
    }
    pages[page]()
    st.html(footer_html(document, PAGES))


# --- Pages ------------------------------------------------------------------


def _network_image(origin: str, horizon: str, stations: list[dict[str, Any]]) -> str:
    """The landing page's network map, drawn once per forecast update and horizon."""
    import streamlit as st

    @st.cache_data(show_spinner=False, max_entries=4)
    def draw(origin: str, horizon: str, _stations: list[dict[str, Any]]) -> str:
        return network_map_uri(_stations, "bike", horizon)

    return draw(origin, horizon, stations)


def _home(
    st: Any,
    document: dict[str, Any],
    stations: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    names: list[str],
    steps: list[str],
    status: str,
    go: Callable[[str], Callable[[], None]],
) -> None:
    """The landing page: hero, why, features, how it works, call to action."""
    far = steps[-1]
    text, visual = st.columns([1, 1.08], gap="large", vertical_alignment="center")
    with text:
        st.html(hero_text_html(document, status))
        with st.container(horizontal=True, key="hero-ctas"):
            st.button("Open the map", type="primary", on_click=go("Map"), key="hero-map")
            st.button("Find a bike now", on_click=go("Go now"), key="hero-go")
    visual.html(hero_visual_html(_network_image(document["origin"], far, stations), stations, far))

    st.html(story_html(stations))

    st.html(
        section_head_html(
            "What you can do",
            "Four ways to ride with confidence.",
            "Each view answers one question about the network, from the whole city "
            "down to a single station.",
        )
    )
    features = list(FEATURES)
    for row in (features[:2], features[2:]):
        for column, page in zip(st.columns(2, gap="medium"), row, strict=True):
            with column, st.container(key=f"card-feature-{PAGES[page]}"):
                st.html(feature_html(page))
                st.button(
                    f"{FEATURES[page][3]} →",
                    type="tertiary",
                    on_click=go(page),
                    key=f"feature-{PAGES[page]}",
                )

    st.html('<div class="vf-section" aria-hidden="true"></div>')
    centre = by_name[names[nearest_index(names, by_name, *CENTRE)]]
    how, demo = st.columns([1, 1.05], gap="large", vertical_alignment="center")
    with how:
        st.html(steps_html(WALK_LIMITS_MIN))
        st.button("Try Go now", type="primary", on_click=go("Go now"), key="demo-go")
    with demo, st.container(key="card-demo"):
        st.html(demo_card_html(centre, steps[0]))

    with st.container(key="card-cta"):
        background = cta_background_html()
        if background:
            st.html(background)
        st.html(cta_html())
        st.button("Open the map", type="primary", on_click=go("Map"), key="cta-map")


def _map(
    st: Any,
    stations: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    by_id: dict[int, dict[str, Any]],
    steps: list[str],
    summary: dict[str, float],
    go: Callable[[str], Callable[[], None]],
) -> None:
    """F1: every station on a map, coloured by its chance at the chosen horizon."""
    state = st.session_state
    st.html(
        page_head_html(
            "Vélib' station forecasts",
            "Real-time and predicted availability across Paris and its suburbs.",
            eyebrow="Map",
        )
    )
    with st.container(horizontal=True, key="toolbar-map"):
        need = NEEDS[st.radio("Looking for", list(NEEDS), horizontal=True, key="map_need")]
        horizon = st.radio(
            "In", steps, horizontal=True, key="map_h", format_func=lambda h: f"{h} min"
        )
    thing = "bike" if need == "bike" else "dock"
    chances = [chance(s, need, horizon) for s in stations if horizon in s["forecasts"]]
    average = sum(chances) / len(chances) if chances else 0.0
    st.html(
        stats_html(
            [
                stat_html("bike", ACCENT, f"{summary['stations']:,.0f}", "Stations forecast"),
                stat_html(
                    "pie",
                    COUNT_COLOURS["ebike"],
                    percent(summary["with_bike"]),
                    "Stations with a bike now",
                ),
                stat_html(
                    "clock",
                    COUNT_COLOURS["docks"],
                    percent(average),
                    f"Average chance of a {thing} in {horizon} min",
                ),
            ]
        )
    )
    left, right = st.columns([1.65, 1], gap="medium")
    selected = by_name.get(state["station"])
    with left:
        event = st.pydeck_chart(
            _deck(
                map_points(stations, need, horizon),
                centre=PARIS_VIEW,
                zoom=11.6,
                highlight=selected,
            ),
            height=560,
            on_select="rerun",
            selection_mode="single-object",
        )
        st.html(legend_html_for(need))
        picked = (event.selection.objects.get("stations") or []) if event else []
        if picked and picked[0]["station_id"] in by_id:
            chosen = by_id[picked[0]["station_id"]]["name"]
            if chosen != state["station"]:
                state["station"] = chosen
                st.rerun()
    with right, st.container(key="card-map-station"):
        if selected is not None:
            st.html(
                station_head_html(selected)
                + f'<div class="vf-subtitle"><b>Forecast in {horizon} min</b>'
                "<small>tap a station on the map</small></div>"
                + count_cards_html(selected, horizon)
                + risk_cards_html(selected, horizon)
                + horizon_table_html(selected)
            )
            st.button(
                "Open station page", width="stretch", on_click=go("Station"), key="open-station"
            )

    chart, figures, tip = st.columns([1.6, 1, 1], gap="medium")
    with chart, st.container(key="card-network-trend"):
        st.html(
            '<div class="vf-card-title">Network outlook<small>share of stations with a bike'
            "</small></div>"
        )
        st.altair_chart(
            _trend_chart(network_trend(stations), share=True, colour=ACCENT), width="stretch"
        )
    with figures, st.container(key="card-network-figures"):
        st.html(network_figures_html(summary))
    with tip, st.container(key="card-callout-info"):
        st.html(tip_html("Smart tip", smart_tip(stations, need, horizon)))


def legend_html_for(need: Need, extra: tuple[tuple[str, str], ...] = ()) -> str:
    """The colour key of a chance map, worded for a bike or a dock."""
    return legend_html(band_labels(f" of a {'bike' if need == 'bike' else 'dock'}"), extra)


def _go_now(
    st: Any,
    stations: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    names: list[str],
    steps: list[str],
) -> None:
    """F2: the best station within walking distance, with alternatives and directions."""
    state = st.session_state
    st.html(
        page_head_html(
            "Go now",
            "The best Vélib' station near you, for the moment you get there.",
            eyebrow="Go now",
        )
    )
    with st.container(horizontal=True, key="toolbar-go"):
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
        walk = st.radio(
            "Walking time",
            WALK_LIMITS_MIN,
            horizontal=True,
            key="walk",
            format_func=lambda m: f"{m} min",
        )
    # The forecast nearest to the walking time: the station as you reach it.
    go_h = min(steps, key=lambda h: abs(int(h) - walk))
    options = options_near(stations, here["lat"], here["lon"], need, go_h, walk * WALK_M_PER_MIN)
    best = best_option(options, GOOD) or (options[0] if options else None)
    thing = "bike" if need == "bike" else "dock"

    card, map_column = st.columns([1, 1.45], gap="medium")
    with card, st.container(key="card-best"):
        if best is None:
            st.html(empty_html("No station in reach", "Choose a longer walking time to see more."))
        else:
            station = best["station"]
            minutes = walk_minutes(best["distance_m"])
            st.html(
                '<div class="vf-demo-label">Best bet</div>'
                + station_head_html(station, f" · {best['distance_m']:.0f} m · ~{minutes} min walk")
                + f'<div class="vf-demo-chance"><b>{percent(chance(station, need, go_h))}</b>'
                f"<span>chance of a {thing} in {go_h} min</span></div>"
                + now_compact_html(station)
                + horizon_table_html(station)
                + risk_cards_html(station, go_h)
            )
            st.link_button(
                "Get directions",
                directions_url(station["lat"], station["lon"]),
                icon=":material/near_me:",
                type="primary",
                width="stretch",
            )
    with map_column:
        st.pydeck_chart(
            _deck(
                map_points(stations, need, go_h),
                centre=(here["lat"], here["lon"]),
                zoom=14.6,
                highlight=best["station"] if best else None,
                origin=here,
            ),
            height=600,
        )
        st.html(legend_html_for(need, ((theme.SECONDARY, "You"),)))
    with st.container(key="card-options"):
        st.html(
            f'<div class="vf-card-title">Stations within a {walk}-minute walk'
            f"<small>chance of a {thing} in {go_h} min · nearest first</small></div>"
            + options_html(options, need, grid=True)
        )


def _at_risk(st: Any, stations: list[dict[str, Any]], steps: list[str]) -> None:
    """F3: the stations most likely to be empty, or full."""
    head = st.empty()  # the title depends on the controls below it
    with st.container(horizontal=True, key="toolbar-risk"):
        need = NEEDS[st.radio("Looking for", list(NEEDS), horizontal=True, key="risk_need")]
        horizon = st.radio(
            "In", steps, horizontal=True, key="risk_h", format_func=lambda h: f"{h} min"
        )
    event_name = NEED_EVENT[need]
    ranked = critical(stations, event_name, horizon, limit=TOP_AT_RISK)
    head.html(
        page_head_html(
            "At risk",
            f"The {len(ranked)} stations most likely to be {event_name} in {horizon} minutes.",
            eyebrow="At risk",
        )
    )
    average = sum(p for p, _ in ranked) / len(ranked) if ranked else 0.0
    likely = sum(1 for p, _ in ranked if p >= 0.5)
    st.html(
        stats_html(
            [
                stat_html("alert", theme.ERROR, f"{likely}", f"More likely {event_name} than not"),
                stat_html("pie", ACCENT, percent(average), f"Average P({event_name}), top list"),
                stat_html("clock", COUNT_COLOURS["docks"], f"{horizon} min", "Forecast horizon"),
            ]
        )
    )
    table, map_column = st.columns([1, 1.2], gap="medium")
    table.html(risk_table_html(ranked, horizon, event_name))
    risky = {s["station_id"] for _, s in ranked}
    points = map_points(stations, need, horizon)
    for point in points:
        point["colour"] = RED if point["station_id"] in risky else theme.MAP_MUTED
    others = [p for p in points if p["station_id"] not in risky]
    at_risk = [p for p in points if p["station_id"] in risky]
    with map_column, st.container(key="sticky-risk-map"):
        st.pydeck_chart(_deck(others, centre=PARIS_VIEW, zoom=11.6, emphasis=at_risk), height=640)
        st.html(
            f'<div class="vf-legend"><span><i style="background:{hex_colour(RED)}"></i>'
            f'Top {len(ranked)} at risk</span><span><i style="background:'
            f'{hex_colour(theme.MAP_MUTED)}"></i>Other stations</span></div>'
        )


def _station(
    st: Any,
    stations: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    names: list[str],
) -> None:
    """F4: one station, its forecast at every horizon, risks and trend."""
    state = st.session_state
    station = by_name[state["station"]]
    head, pick = st.columns([2, 1], vertical_alignment="bottom", gap="medium")
    head.html(
        f'<header class="vf-head" style="padding-bottom:0"><div class="vf-eyebrow">Station</div>'
        f"{station_head_html(station, xl=True)}</header>"
    )
    pick.selectbox(
        "Change station",
        names,
        index=names.index(state["station"]),
        key="station_pick",
        on_change=lambda: state.update(station=state["station_pick"]),
    )
    st.html(now_tiles_html(station))
    map_column, forecast = st.columns([1.3, 1], gap="medium")
    with map_column:
        st.pydeck_chart(
            _deck(
                map_points(stations, "bike", horizons([station])[0]),
                centre=(station["lat"], station["lon"]),
                zoom=14.8,
                highlight=station,
                faded=True,
            ),
            height=470,
        )
    with forecast, st.container(key="card-station-forecast"):
        horizon = st.radio(
            "Forecast",
            horizons([station]),
            horizontal=True,
            key="station_h",
            format_func=lambda h: f"In {h} min",
        )
        st.html(
            count_cards_html(station, horizon)
            + risk_cards_html(station, horizon)
            + horizon_table_html(station)
        )
    trend, reading = st.columns([1.6, 1], gap="medium")
    with trend, st.container(key="card-station-trend"):
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
    with reading, st.container(key=f"card-callout-{mood}"):
        st.html(tip_html("Insight", text, mood))


def _admin(st: Any, document: dict[str, Any]) -> None:
    """F6: service health, read from the document's monitoring block."""
    st.html(
        page_head_html(
            "Service health",
            "Collector, model in service and real accuracy, measured at each forecast run.",
            eyebrow="Admin",
        )
    )
    monitoring = document.get("status")
    if monitoring is None:
        st.info("This forecast document has no monitoring block yet.")
        return
    st.html(alerts_html(monitoring))
    st.html(health_html(monitoring, datetime.now(UTC)))
    st.html(accuracy_html(monitoring))
    st.html(events_html(monitoring))
    st.html(models_html(monitoring))


def _about(st: Any, document: dict[str, Any]) -> None:
    """How the forecasts are made, every figure read from the document."""
    st.html(
        page_head_html(
            "About VéloFlow",
            "How the forecasts are made, and what they are worth.",
            eyebrow="About",
        )
    )
    st.html(about_html(document))


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
        "--server.enableStaticServing",
        "true",  # fonts and pictures under static/, served at app/static/
        "--browser.gatherUsageStats",
        "false",
        *[arg for key, value in THEME.items() for arg in (f"--theme.{key}", value)],
    ]
    sys.exit(cli.main())


if __name__ == "__main__":
    render()
