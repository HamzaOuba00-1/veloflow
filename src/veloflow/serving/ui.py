"""Building blocks of the web app: figures and the HTML of its cards.

Figures are computed from a forecast document; cards are HTML strings.

Pure functions, tested without a browser. Every text that comes from the feed
(station names) is escaped. Icons are inline SVG in one line style, so the
app needs no icon font and no image.
"""

import base64
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from veloflow.serving.queries import NEED_EVENT, Need, distance_m

PARIS = ZoneInfo("Europe/Paris")
HORIZONS = ("15", "30", "60")
CENTRE = (48.8584, 2.3470)  # Châtelet
GOOD, FAIR = 0.8, 0.5
DASH = "\u2013"  # en dash, for ranges
# Status colours (map, chances) and the accent of the interface.
GREEN, ORANGE, RED = [34, 197, 94, 235], [245, 158, 11, 235], [239, 68, 68, 235]
TONES = {"good": "#16a34a", "fair": "#d97706", "poor": "#dc2626"}
NEUTRAL = "#94a3b8"
ACCENT = "#6366f1"
COUNTS = (
    ("mechanical", "Mechanical bikes", "bike"),
    ("ebike", "E-bikes", "zap"),
    ("docks", "Free docks", "dock"),
)
COUNT_COLOURS = {"mechanical": "#4f6bed", "ebike": "#8b5cf6", "docks": "#16a34a"}

_PATHS = {
    "bike": '<circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/>'
    '<path d="M15 6h2l3 11.5M5.5 17.5 9 9h7l-4.5 8.5H5.5M9 9 8 6H6"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "dock": '<path d="M5 21V8M19 21V8M12 21V3M3 21h18M9 8h6M5 8h14"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17'
    'a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
    '<path d="M12 9v4M12 17h.01"/>',
    "pin": '<path d="M12 22s7-6.1 7-12a7 7 0 0 0-14 0c0 5.9 7 12 7 12z"/>'
    '<circle cx="12" cy="10" r="2.5"/>',
    "nav": '<path d="M3 11 22 2l-9 19-2-8-8-2z"/>',
    "map": '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "bulb": '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6'
    'c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/>',
    "check": '<path d="m5 12 5 5L20 7"/>',
    "walk": '<circle cx="13" cy="4" r="2"/><path d="m9 21 3-6-3-3 1-5 4 3 3 1M7 12l2-5"/>',
    "pie": '<path d="M21 12A9 9 0 1 1 12 3v9z"/><path d="M15 3.5A9 9 0 0 1 20.5 9H15z"/>',
}


def icon(name: str, size: int = 20, colour: str = "#475569") -> str:
    """An icon as an inline image (24 x 24 grid, 1.8 px stroke).

    Streamlit sanitises ``st.html`` and drops ``<svg>`` elements, but keeps
    images with a ``data:`` URI: the SVG travels base64-encoded inside one.
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 24 24" fill="none" stroke="{colour}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>'
    )
    encoded = base64.b64encode(svg.encode()).decode()
    return (
        f'<img class="vf-icon" src="data:image/svg+xml;base64,{encoded}" width="{size}" '
        f'height="{size}" alt="">'
    )


# --- Figures --------------------------------------------------------------


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


def district(code: str | None) -> str:
    """``Paris 3e`` from a station code; ``Métropole`` outside Paris."""
    try:
        number = int(code or "")
    except ValueError:
        return "Métropole"
    if number // 1000 == 60:  # Invalides and Concorde, on the Seine banks
        return "Paris 7e" if number == 60001 else "Paris 8e"
    arrondissement = number // 1000
    if 1 <= arrondissement <= 20:
        return f"Paris {arrondissement}{'er' if arrondissement == 1 else 'e'}"
    return "Métropole"


def chance(station: dict[str, Any], need: Need, horizon: str) -> float:
    """Chance to find a bike (``1 - P(empty)``) or a dock (``1 - P(full)``)."""
    return 1.0 - float(station["forecasts"][horizon][f"p_{NEED_EVENT[need]}"])


def network_summary(stations: list[dict[str, Any]]) -> dict[str, float]:
    """Bikes, e-bikes and docks available now, and the share of stations with a bike."""
    total = len(stations)
    with_bike = sum(1 for s in stations if s["now"]["mechanical"] + s["now"]["ebike"] > 0)
    return {
        "stations": total,
        "mechanical": sum(s["now"]["mechanical"] for s in stations),
        "ebike": sum(s["now"]["ebike"] for s in stations),
        "docks": sum(s["now"]["docks"] for s in stations),
        "with_bike": with_bike / total if total else 0.0,
    }


def network_trend(stations: list[dict[str, Any]]) -> list[tuple[str, float]]:
    """Share of stations with a bike now, then expected at each horizon."""
    if not stations:
        return []
    points = [("Now", network_summary(stations)["with_bike"])]
    for horizon in HORIZONS:
        chances = [chance(s, "bike", horizon) for s in stations if horizon in s["forecasts"]]
        if chances:
            points.append((f"{horizon} min", sum(chances) / len(chances)))
    return points


def station_trend(station: dict[str, Any], count: str) -> list[tuple[str, float]]:
    """A count now, then forecast at each horizon."""
    points = [("Now", float(station["now"][count]))]
    points += [
        (f"{h} min", float(station["forecasts"][h][count]))
        for h in HORIZONS
        if h in station["forecasts"]
    ]
    return points


def smart_tip(stations: list[dict[str, Any]], need: Need, horizon: str) -> str:
    """Where the risk concentrates: the district with the most stations at risk."""
    event = NEED_EVENT[need]
    by_district: dict[str, int] = {}
    for s in stations:
        if horizon in s["forecasts"] and s["forecasts"][horizon][f"p_{event}"] >= FAIR:
            by_district[district(s["code"])] = by_district.get(district(s["code"]), 0) + 1
    if not by_district:
        thing = "bikes" if need == "bike" else "free docks"
        return f"No district is short of {thing} in the next {horizon} minutes."
    worst, count = max(by_district.items(), key=lambda item: (item[1], item[0]))
    what = "run out of bikes" if need == "bike" else "fill up"
    plural = "station is" if count == 1 else "stations are"
    return f"{count} {plural} likely to {what} in {worst} within {horizon} minutes."


def insight(station: dict[str, Any], horizon: str) -> tuple[str, str]:
    """A one-line reading of a station's risks, and its tone."""
    forecast = station["forecasts"][horizon]
    empty, full = forecast["p_empty"], forecast["p_full"]
    if empty >= FAIR:
        return f"High risk of no bike in {horizon} minutes ({percent(empty)}).", "poor"
    if full >= FAIR:
        return f"High risk of no free dock in {horizon} minutes ({percent(full)}).", "poor"
    if max(empty, full) >= 0.2:
        what = "empty" if empty >= full else "full"
        return (
            f"Some risk of the station being {what} in {horizon} minutes "
            f"({percent(max(empty, full))}).",
            "fair",
        )
    return f"Low risk of being empty or full in the next {horizon} minutes.", "good"


def nearest_index(
    names: list[str], by_name: dict[str, dict[str, Any]], lat: float, lon: float
) -> int:
    """Position in ``names`` of the station nearest to a point."""
    return min(
        range(len(names)),
        key=lambda i: distance_m(lat, lon, by_name[names[i]]["lat"], by_name[names[i]]["lon"]),
    )


def directions_url(lat: float, lon: float) -> str:
    """Walking directions to a point, in Google Maps."""
    return (
        f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=walking"
    )


def map_points(stations: list[dict[str, Any]], need: Need, horizon: str) -> list[dict[str, Any]]:
    """One point per station: position, chance of success, colour, tooltip text."""
    points = []
    for s in stations:
        if horizon not in s["forecasts"]:
            continue
        p = chance(s, need, horizon)
        now = s["now"]
        points.append(
            {
                "station_id": s["station_id"],
                "lat": s["lat"],
                "lon": s["lon"],
                "name": s["name"],
                "chance": percent(p),
                "now": f"{now['mechanical']:.0f} mechanical · {now['ebike']:.0f} e-bikes · "
                f"{now['docks']:.0f} docks",
                "colour": colour(p),
            }
        )
    return points


# --- HTML -----------------------------------------------------------------


def _bar(value: float, fill: str) -> str:
    width = max(0.0, min(1.0, value)) * 100
    return f'<div class="vf-bar"><span style="width:{width:.1f}%;background:{fill}"></span></div>'


def topbar_html(document: dict[str, Any], status: str, age_s: float) -> str:
    """Live (or delayed) pill with the time of the data."""
    origin = datetime.fromisoformat(document["origin"]).astimezone(PARIS)
    stale = status == "stale"
    label = "Delayed data" if stale else "Live data"
    sub = f"{age_s / 60:.0f} min old" if stale else f"Updated {origin:%H:%M}"
    return (
        f'<div class="vf-live {"stale" if stale else ""}"><span class="vf-dot"></span>'
        f"<div><b>{label}</b><small>{sub}</small></div></div>"
    )


def sidebar_status_html(document: dict[str, Any], status: str) -> str:
    """The live data card at the bottom of the sidebar."""
    origin = datetime.fromisoformat(document["origin"]).astimezone(PARIS)
    stale = status == "stale"
    return (
        f'<div class="vf-side-live {"stale" if stale else ""}"><span class="vf-dot"></span>'
        f"<div><b>{'Delayed data' if stale else 'Live data'}</b>"
        f"<small>Updated {origin:%H:%M}</small></div></div>"
    )


def brand_html() -> str:
    """Logo and name at the top of the sidebar."""
    return (
        f'<div class="vf-brand">{icon("bike", 44, "#ffffff")}'
        '<div class="vf-brand-name">VéloFlow</div>'
        '<div class="vf-brand-sub">Smart Vélib\' forecasts</div></div>'
    )


def page_head_html(icon_name: str, title: str, subtitle: str, eyebrow: str | None = None) -> str:
    """Title block of a page."""
    chip = (
        f'<span class="vf-chip-accent">{icon("pin", 14)} {escape(eyebrow)}</span>'
        if eyebrow
        else ""
    )
    return (
        f'<div class="vf-page-head">{chip}<div class="vf-page-title"><span class="vf-page-icon">'
        f"{icon(icon_name, 30, ACCENT)}</span><div><h1>{escape(title)}</h1>"
        f"<p>{escape(subtitle)}</p></div></div></div>"
    )


def kpi_html(icon_name: str, tint: str, value: str, label: str, note: str = "") -> str:
    """A key figure with its icon."""
    extra = f'<div class="vf-kpi-note">{escape(note)}</div>' if note else ""
    return (
        f'<div class="vf-card vf-kpi"><div class="vf-kpi-icon" style="background:{tint}1f">'
        f'{icon(icon_name, 26, tint)}</div><div><div class="vf-kpi-value">{escape(value)}</div>'
        f'<div class="vf-kpi-label">{escape(label)}</div>{extra}</div></div>'
    )


def count_cards_html(station: dict[str, Any], horizon: str) -> str:
    """Forecast of the three counts at a horizon, each with its 80 % interval."""
    forecast = station["forecasts"][horizon]
    cards = []
    for key, label, icon_name in COUNTS:
        low, high = forecast[f"{key}_interval"]
        capacity = max(1.0, float(station.get("capacity") or 0) or high or 1.0)
        cards.append(
            f'<div class="vf-count" style="--tint:{COUNT_COLOURS[key]}">'
            f'{icon(icon_name, 24, COUNT_COLOURS[key])}<div class="vf-count-label">{label}</div>'
            f'<div class="vf-count-value">{forecast[key]:.0f}</div>'
            f'<div class="vf-count-note">likely {low:.0f}{DASH}{high:.0f}</div>'
            f"{_bar(forecast[key] / capacity, COUNT_COLOURS[key])}</div>"
        )
    return f'<div class="vf-counts">{"".join(cards)}</div>'


def risk_cards_html(station: dict[str, Any], horizon: str) -> str:
    """Probabilities that the station is empty, or full, at a horizon."""
    forecast = station["forecasts"][horizon]
    cards = "".join(
        f'<div class="vf-risk"><span class="vf-ring" style="border-color:{risk_colour(p)}"></span>'
        f'<div class="vf-risk-body"><div class="vf-risk-value">{percent(p)}</div>'
        f'<div class="vf-risk-label">{label}</div>{_bar(p, risk_colour(p))}</div></div>'
        for label, p in (
            ("Station empty", forecast["p_empty"]),
            ("Station full", forecast["p_full"]),
        )
    )
    return f'<div class="vf-risks">{cards}</div>'


def station_head_html(station: dict[str, Any], extra: str = "") -> str:
    """Name and district of a station."""
    return (
        f'<div class="vf-station-head"><span class="vf-pin">{icon("pin", 22, ACCENT)}</span><div>'
        f'<div class="vf-station-name">{escape(station["name"])}</div>'
        f'<div class="vf-station-sub">{district(station["code"])}{extra}</div></div></div>'
    )


def now_tiles_html(station: dict[str, Any]) -> str:
    """What the station holds now, with the change expected in 15 minutes."""
    tiles = []
    for key, label, icon_name in COUNTS:
        now = station["now"][key]
        later = station["forecasts"].get("15", {}).get(key)
        delta = ""
        if later is not None:
            change = round(later - now)
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            delta = f'<div class="vf-kpi-note">{arrow} {change:+d} in 15 min</div>'
        tiles.append(
            f'<div class="vf-card vf-kpi"><div class="vf-kpi-icon" style="background:'
            f'{COUNT_COLOURS[key]}1f">{icon(icon_name, 26, COUNT_COLOURS[key])}</div><div>'
            f'<div class="vf-kpi-value">{now:.0f}</div><div class="vf-kpi-label">{label}</div>'
            f"{delta}</div></div>"
        )
    return f'<div class="vf-kpis">{"".join(tiles)}</div>'


def now_compact_html(station: dict[str, Any]) -> str:
    """What a station holds now, in three compact tiles (icon, number, label)."""
    tiles = "".join(
        f'<div class="vf-mini" style="--tint:{COUNT_COLOURS[key]}">'
        f"{icon(icon_name, 22, COUNT_COLOURS[key])}"
        f'<div class="vf-mini-value">{station["now"][key]:.0f}</div>'
        f'<div class="vf-mini-label">{label}</div></div>'
        for key, label, icon_name in COUNTS
    )
    return f'<div class="vf-minis">{tiles}</div>'


def horizon_table_html(station: dict[str, Any]) -> str:
    """Mechanical / e-bikes / docks forecast at 15, 30 and 60 minutes."""
    cells = "".join(
        f'<div class="vf-hcell"><div class="vf-hcell-h">{h} min</div><div class="vf-hcell-v">'
        f"{station['forecasts'][h]['mechanical']:.0f} / {station['forecasts'][h]['ebike']:.0f} / "
        f"{station['forecasts'][h]['docks']:.0f}</div></div>"
        for h in HORIZONS
        if h in station["forecasts"]
    )
    return (
        f'<div class="vf-section-title">{icon("clock", 18)} Next 15 / 30 / 60 min '
        f'<small>mechanical / e-bikes / docks</small></div><div class="vf-hgrid">{cells}</div>'
    )


def tip_html(title: str, text: str, tone_name: str = "fair") -> str:
    """A highlighted tip or insight."""
    mark = icon("check", 22, "#ffffff") if tone_name == "good" else icon("bulb", 22, "#ffffff")
    return (
        f'<div class="vf-card vf-tip {tone_name}"><span class="vf-tip-icon">{mark}</span>'
        f"<div><b>{escape(title)}</b><p>{escape(text)}</p></div></div>"
    )


def network_figures_html(summary: dict[str, float]) -> str:
    """Bikes, e-bikes and docks available across the network now."""
    items = (
        ("bike", COUNT_COLOURS["mechanical"], f"{summary['mechanical']:,.0f}", "Mechanical bikes"),
        ("zap", COUNT_COLOURS["ebike"], f"{summary['ebike']:,.0f}", "E-bikes"),
        ("dock", COUNT_COLOURS["docks"], f"{summary['docks']:,.0f}", "Free docks"),
    )
    return (
        '<div class="vf-netfig">'
        + "".join(
            f"<div>{icon(name, 26, tint)}<div><b>{value}</b><small>{label}</small></div></div>"
            for name, tint, value, label in items
        )
        + "</div>"
    )


def risk_table_html(ranked: list[tuple[float, dict[str, Any]]], horizon: str, event: str) -> str:
    """The at-risk stations, most likely first."""
    if not ranked:
        return f'<div class="vf-card vf-muted">No station is likely to be {event} right now.</div>'
    rows = "".join(
        f'<tr><td class="vf-num">{i}</td><td>{escape(s["name"])}</td>'
        f'<td class="vf-muted">{district(s["code"])}</td><td><span class="vf-badge" '
        f'style="color:{risk_colour(p)};background:{risk_colour(p)}1a">{percent(p)}</span></td></tr>'
        for i, (p, s) in enumerate(ranked, start=1)
    )
    return (
        f'<div class="vf-card vf-table-card"><div class="vf-section-title">{icon("chart", 18)} '
        f'Top {len(ranked)} at-risk stations</div><table class="vf-table"><thead><tr><th>#</th>'
        f"<th>Station</th><th>District</th><th>{event.capitalize()} in {horizon} min</th></tr>"
        f"</thead><tbody>{rows}</tbody></table></div>"
    )


def options_html(options: list[dict[str, Any]], need: Need) -> str:
    """Stations within reach, nearest first, with the chance of success."""
    if not options:
        return '<div class="vf-muted">No station within this walking distance.</div>'
    thing = "bike" if need == "bike" else "dock"
    rows = "".join(
        f'<div class="vf-opt"><div><div class="vf-opt-name">{escape(o["station"]["name"])}</div>'
        f"<small>{o['distance_m']:.0f} m · ~{max(1, round(o['distance_m'] / 80))} min walk</small>"
        f'</div><span class="vf-badge" style="color:{TONES[tone(o["probability"])]};background:'
        f'{TONES[tone(o["probability"])]}1a">{percent(o["probability"])} {thing}</span></div>'
        for o in options[:8]
    )
    return f'<div class="vf-opts">{rows}</div>'


def legend_html(labels: tuple[str, str, str]) -> str:
    """Colour key of a map."""
    swatches = ("#22c55e", "#f59e0b", "#ef4444")
    return (
        '<div class="vf-legend">'
        + "".join(
            f'<span><i style="background:{c}"></i>{escape(label)}</span>'
            for c, label in zip(swatches, labels, strict=True)
        )
        + "</div>"
    )
