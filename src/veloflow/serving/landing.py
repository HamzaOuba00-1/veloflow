"""The landing page (Home): what VéloFlow does, shown on the live forecasts.

Every figure on the page is read from the forecast document (stations,
horizons, counts, chances); nothing is written by hand. Pictures are optional:
each slot has a fallback drawn from the data, so the page is complete before
its photos exist (see ``IMAGE_SLOTS``).

Pure functions returning HTML, tested without a browser. The buttons of the
page are Streamlit widgets placed by ``app.py`` between these blocks.
"""

import base64
import math
from html import escape
from typing import Any

from veloflow.serving import theme
from veloflow.serving.queries import Need
from veloflow.serving.ui import (
    ACCENT,
    GITHUB,
    brand_html,
    chance,
    colour,
    hex_colour,
    horizon_table_html,
    horizons,
    icon,
    network_trend,
    percent,
    risk_cards_html,
    series,
    station_head_html,
    updated_at,
)

# Optional photos: file stem in static/images -> what the fallback shows.
IMAGE_SLOTS = {
    "landing-ride": "the 'now vs later' panel of the story section",
    "landing-cta": "the violet gradient of the closing call to action",
}

FEATURES = {
    # page -> (icon, title, text, button)
    "Map": (
        "map",
        "Live forecast map",
        "Every station coloured by its chance of a bike, or of a free dock, at the horizon "
        "you choose. Tap a station for its full forecast.",
        "Open the map",
    ),
    "Go now": (
        "nav",
        "Go now",
        "Say where you are and how far you will walk: VéloFlow points to the safest station "
        "in reach, with walking directions.",
        "Find a station",
    ),
    "At risk": (
        "alert",
        "Stations at risk",
        "The stations most likely to run out of bikes, or to fill up, ranked so you can "
        "steer clear of them.",
        "See the ranking",
    ),
    "Station": (
        "pin",
        "Station page",
        "Counts and their likely range at every horizon, the risk of empty or full, and "
        "how availability is expected to move.",
        "Open a station",
    ),
}


def network_map_uri(stations: list[dict[str, Any]], need: Need, horizon: str) -> str:
    """Every station as a dot coloured by its chance, as an SVG data URI.

    A plain equirectangular projection around the network's mean latitude is
    exact enough at this scale (about 25 km). Riskier stations are drawn last,
    on top. Streamlit drops inline ``<svg>`` but keeps ``data:`` images.
    """
    placed = [s for s in stations if horizon in s["forecasts"]]
    if not placed:
        return ""
    lats = [s["lat"] for s in placed]
    lons = [s["lon"] for s in placed]
    squeeze = math.cos(math.radians(sum(lats) / len(lats)))
    width_deg = (max(lons) - min(lons)) * squeeze or 1.0
    height_deg = (max(lats) - min(lats)) or 1.0
    scale = 1000 / max(width_deg, height_deg)
    width, height = width_deg * scale, height_deg * scale
    groups: dict[str, list[str]] = {}
    for s in placed:
        x = (s["lon"] - min(lons)) * squeeze * scale
        y = (max(lats) - s["lat"]) * scale
        fill = hex_colour(colour(chance(s, need, horizon)))
        groups.setdefault(fill, []).append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4.6"/>')
    # Most common colour first: the rarer (riskier) dots stay visible on top.
    order = sorted(groups, key=lambda f: -len(groups[f]))
    body = "".join(f'<g fill="{f}">{"".join(groups[f])}</g>' for f in order)
    pad = 12
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad} {-pad} '
        f'{width + 2 * pad:.0f} {height + 2 * pad:.0f}">{body}</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def hero_text_html(document: dict[str, Any], status: str) -> str:
    """Live pill, headline and lead of the hero."""
    stations = document["stations"]
    live = (
        "Delayed data" if status == "stale" else f"Live forecasts · updated {updated_at(document)}"
    )
    dot = ' style="background:var(--vf-color-warning);animation:none"' if status == "stale" else ""
    return (
        f'<div class="vf-hero-text"><span class="vf-pill"><span class="vf-dot"{dot}></span>'
        f"{escape(live)}</span>"
        '<h1 class="vf-display">Know before<br>you <em>go</em>.</h1>'
        '<p class="vf-lead">VéloFlow forecasts mechanical bikes, e-bikes and free docks at '
        f"every one of the {len(stations):,} Vélib' stations, "
        f"{series(horizons(stations))} minutes ahead, so the station you walk to still has "
        "what you need when you get there.</p></div>"
    )


def hero_visual_html(map_uri: str, stations: list[dict[str, Any]], horizon: str) -> str:
    """The live network map, with its key figure and colour key floating over it."""
    with_bike = [chance(s, "bike", horizon) for s in stations if horizon in s["forecasts"]]
    average = sum(with_bike) / len(with_bike) if with_bike else 0.0
    image = (
        f'<img class="vf-network" src="{map_uri}" alt="Map of the {len(stations):,} Vélib\' '
        f'stations, coloured by their chance of a bike in {horizon} minutes">'
        if map_uri
        else ""
    )
    good, fair, poor = (hex_colour(colour(p)) for p in (1.0, 0.6, 0.0))
    return (
        f'<div class="vf-visual">{image}'
        f'<div class="vf-float tl"><span class="vf-icon-tile">{icon("bike", 20, ACCENT)}</span>'
        f"<div><b>{percent(average)}</b><small>average chance of a bike in {horizon} min"
        "</small></div></div>"
        '<div class="vf-float br"><div class="vf-legend">'
        f'<span><i style="background:{good}"></i>Likely</span>'
        f'<span><i style="background:{fair}"></i>Uncertain</span>'
        f'<span><i style="background:{poor}"></i>Unlikely</span></div></div></div>'
    )


def story_html(stations: list[dict[str, Any]]) -> str:
    """Why: the station you see now is not the one you reach. Now vs the last horizon."""
    trend = network_trend(stations)
    now_share = trend[0][1] if trend else 0.0
    later_label, later_share = trend[-1] if len(trend) > 1 else ("", 0.0)
    photo = theme.image_url("landing-ride")
    if photo:
        media = (
            f'<img src="{photo}" alt="A rider taking a Vélib\' bike from a station in Paris" '
            'loading="lazy">'
        )
    else:
        media = (
            '<div class="vf-compare"><div><small>NOW</small>'
            f"<b>{percent(now_share)}</b><span>of stations have a bike</span></div>"
            f"<div><small>IN {escape(later_label.upper())}</small><b>{percent(later_share)}</b>"
            "<span>average chance of finding one</span></div></div>"
        )
    return (
        '<section class="vf-section vf-reveal" aria-labelledby="vf-story"><div class="vf-story">'
        '<div><div class="vf-eyebrow">Why VéloFlow</div>'
        '<h2 id="vf-story" class="vf-h2">What a station holds now is not what you will find.</h2>'
        '<p class="vf-lead" style="margin-top:var(--vf-space-4)">The Vélib\' feed tells you '
        "what a station holds at this moment. By the time you walk there it may be empty, "
        "or full when you come to return a bike. VéloFlow looks ahead.</p>"
        '<ul class="vf-checks">'
        f"<li>{icon('check', 20, ACCENT)}<span><b>Counts, with their range.</b> Mechanical "
        "bikes, e-bikes and free docks, each with the range it will likely fall in.</span></li>"
        f"<li>{icon('check', 20, ACCENT)}<span><b>Risk, made explicit.</b> The probability "
        "that the station will be empty, or full.</span></li>"
        f"<li>{icon('check', 20, ACCENT)}<span><b>The whole network.</b> "
        f"{len(stations):,} stations across Paris and the Métropole, refreshed with the "
        "live feed.</span></li></ul></div>"
        f'<div class="vf-story-media">{media}</div></div></section>'
    )


def section_head_html(eyebrow: str, title: str, lead: str, *, align: str = "center") -> str:
    """Eyebrow, title and lead of a landing section."""
    return (
        f'<section class="vf-section vf-reveal"><div class="vf-section-head'
        f'{" left" if align == "left" else ""}"><div class="vf-eyebrow">{escape(eyebrow)}</div>'
        f'<h2 class="vf-h2">{escape(title)}</h2><p class="vf-lead">{escape(lead)}</p></div>'
        "</section>"
    )


def feature_html(page: str) -> str:
    """Icon, title and text of a feature card (its button is a Streamlit widget)."""
    icon_name, title, text, _ = FEATURES[page]
    return (
        f'<div class="vf-feature-top"><span class="vf-icon-tile lg">{icon(icon_name, 24, ACCENT)}'
        f'</span><div><h3 class="vf-h3">{escape(title)}</h3></div><p>{escape(text)}</p></div>'
    )


def steps_html(walk_limits: tuple[int, ...]) -> str:
    """How Go now works, in three steps."""
    walks = series([str(m) for m in walk_limits])
    return (
        '<div class="vf-reveal"><div class="vf-eyebrow">See it work</div>'
        '<h2 class="vf-h2">From where you stand to the right station, in three steps.</h2>'
        '<ol class="vf-steps">'
        "<li><div><b>Say where you are</b><p>Pick the station nearest to you.</p></div></li>"
        "<li><div><b>Choose a bike or a dock, and your walk</b>"
        f"<p>{escape(walks)} minutes on foot.</p></div></li>"
        "<li><div><b>Walk to the safest bet</b><p>Stations in reach are ranked by their "
        "chance at the time you get there, with directions to the best one.</p></div></li>"
        "</ol></div>"
    )


def demo_card_html(station: dict[str, Any], horizon: str) -> str:
    """A live forecast for one station, as Go now shows it."""
    return (
        f'<div class="vf-demo-label"><span class="vf-dot"></span>Live example</div>'
        f"{station_head_html(station)}"
        f'<div class="vf-demo-chance"><b>{percent(chance(station, "bike", horizon))}</b>'
        f"<span>chance of a bike in {horizon} min</span></div>"
        f"{horizon_table_html(station)}{risk_cards_html(station, horizon)}"
    )


def cta_html() -> str:
    """Title and line of the closing call to action."""
    return (
        '<div class="vf-cta"><h2 class="vf-h2">Your next ride, one step ahead.</h2>'
        "<p>Open the map and see where the bikes will be, not just where they are.</p></div>"
    )


def cta_background_html() -> str:
    """The photo behind the closing call to action, or nothing until it is added."""
    photo = theme.image_url("landing-cta")
    return f'<img class="vf-cta-bg" src="{photo}" alt="" loading="lazy">' if photo else ""


def footer_html(document: dict[str, Any], links: dict[str, str]) -> str:
    """Brand, navigation, sources and licences. ``links``: page label -> URL slug."""
    model = document["model"]
    trained = str(model["data_end"])[:16].replace("T", " ")
    app_links = "".join(
        f'<li><a href="?page={slug}">{escape(label)}</a></li>'
        for label, slug in links.items()
        if label not in {"Admin", "About"}
    )
    project = "".join(
        f'<li><a href="?page={links[label]}">{escape(text)}</a></li>'
        for label, text in (("About", "How it works"), ("Admin", "Service health"))
        if label in links
    )
    return (
        '<footer class="vf-footer" role="contentinfo">'
        f"<div>{brand_html(href='?page=' + links.get('Home', 'home'))}"
        "<p>Short-term availability forecasts for Vélib' Métropole, computed from the "
        "public feed.</p></div>"
        f'<nav aria-label="App"><h3>App</h3><ul>{app_links}</ul></nav>'
        f'<nav aria-label="Project"><h3>Project</h3><ul>{project}'
        f'<li><a href="{GITHUB}">Source code</a></li></ul></nav>'
        '<div class="vf-footer-legal"><span>Data: Vélib\' Métropole (ODbL) · Weather: '
        "Open-Meteo (CC BY 4.0) · Code: MIT licence. Not affiliated with Vélib' Métropole."
        f"</span><span>Model {escape(str(model['name']))}, trained on data up to "
        f"{escape(trained)} UTC</span></div></footer>"
    )
