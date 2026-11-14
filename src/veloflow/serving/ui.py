"""Building blocks of the web app: figures and the HTML of its components.

Figures are computed from a forecast document; components are HTML strings
styled by the design system (``theme.py`` tokens, ``static/css/veloflow.css``).

Pure functions, tested without a browser. Every text that comes from the feed
(station names) is escaped. Icons are SVG in one line style, so the app needs
no icon font. Pictures are served from ``static/images`` (see ``theme``).
"""

import base64
import re
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from veloflow.serving import theme
from veloflow.serving.queries import NEED_EVENT, Need, distance_m

PARIS = ZoneInfo("Europe/Paris")
CENTRE = (48.8584, 2.3470)  # Châtelet
# Thresholds and assumptions; every figure shown is computed from them or
# from the forecast document, never written in a text.
GOOD, FAIR = 0.8, 0.5  # chance of success: good from GOOD, fair from FAIR
WATCH = 0.2  # a risk of empty or full is worth showing from WATCH
WALK_M_PER_MIN = 80  # walking speed (4.8 km/h) to turn distances into minutes
DASH = "\u2013"  # en dash, for ranges
# Status colours (map dots, text of chances and risks) and the accent.
GREEN, ORANGE, RED = theme.MAP_GOOD, theme.MAP_FAIR, theme.MAP_POOR
TONES = {"good": theme.SUCCESS, "fair": theme.WARNING, "poor": theme.ERROR}
NEUTRAL = theme.BORDER_STRONG
ACCENT = theme.PRIMARY
COUNT_COLOURS = theme.COUNT_COLOURS
COUNTS = (
    ("mechanical", "Mechanical bikes", "bike"),
    ("ebike", "E-bikes", "zap"),
    ("docks", "Free docks", "dock"),
)
SHORT_LABELS = {"mechanical": "Mechanical", "ebike": "E-bikes", "docks": "Docks"}
FAVICON = theme.STATIC / "images" / "favicon.png"

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
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.4 3.4 8.3 8 9 4.6-.7 8-4.6 8-9V6l-8-3z"/>'
    '<path d="m9 12 2 2 4-4"/>',
}


def icon(name: str, size: int = 20, colour: str = theme.TEXT_SECONDARY) -> str:
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


def horizons(stations: list[dict[str, Any]]) -> list[str]:
    """Forecast horizons present in the document, in minutes, shortest first."""
    found = {h for s in stations for h in s["forecasts"]}
    return sorted(found, key=int)


def station_horizons(station: dict[str, Any]) -> list[str]:
    """Forecast horizons of one station, shortest first."""
    return sorted(station["forecasts"], key=int)


def walk_minutes(distance: float) -> int:
    """Walking time in whole minutes (at least one) at ``WALK_M_PER_MIN``."""
    return max(1, round(distance / WALK_M_PER_MIN))


def band_labels(suffix: str = "") -> tuple[str, str, str]:
    """Legend of the three chance colours, written from ``GOOD`` and ``FAIR``."""
    good, fair = round(GOOD * 100), round(FAIR * 100)
    return (f"{good}%+ chance{suffix}", f"{fair}{DASH}{good}%", f"Under {fair}%")


def percent(probability: float) -> str:
    """A probability for people: never "100 %" or "0 %", which no forecast is."""
    if probability >= 0.995:
        return "> 99%"
    if probability < 0.005:
        return "< 1%"
    return f"{probability:.0%}"


def tone(probability: float) -> str:
    """``good`` from ``GOOD``, ``fair`` from ``FAIR``, ``poor`` below."""
    if probability >= GOOD:
        return "good"
    return "fair" if probability >= FAIR else "poor"


def colour(probability: float) -> list[int]:
    """Map colour of a chance of success."""
    return {"good": GREEN, "fair": ORANGE, "poor": RED}[tone(probability)]


def risk_colour(probability: float) -> str:
    """Colour of a risk (empty or full): red from ``FAIR``, orange from ``WATCH``, else grey."""
    if probability >= FAIR:
        return TONES["poor"]
    return TONES["fair"] if probability >= WATCH else NEUTRAL


def risk_tone(probability: float) -> str:
    """Tone of a risk (empty or full): ``poor`` from ``FAIR``, ``fair`` from ``WATCH``.

    Below ``WATCH`` the tone is empty (neutral).
    """
    if probability >= FAIR:
        return "poor"
    return "fair" if probability >= WATCH else ""


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
    for horizon in horizons(stations):
        chances = [chance(s, "bike", horizon) for s in stations if horizon in s["forecasts"]]
        if chances:
            points.append((f"{horizon} min", sum(chances) / len(chances)))
    return points


def station_trend(station: dict[str, Any], count: str) -> list[tuple[str, float]]:
    """A count now, then forecast at each horizon."""
    points = [("Now", float(station["now"][count]))]
    points += [
        (f"{h} min", float(station["forecasts"][h][count])) for h in station_horizons(station)
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
    if max(empty, full) >= WATCH:
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


def hex_colour(rgba: list[int]) -> str:
    """``#rrggbb`` of a map colour (its alpha left out)."""
    return "#" + "".join(f"{c:02x}" for c in rgba[:3])


def age_text(age_s: float) -> str:
    """How old the forecasts are, for people: ``45 min ago``, ``2 h ago``, ``2 h 05 ago``."""
    minutes = max(0, round(age_s / 60))
    if minutes < 60:
        return f"{minutes} min ago"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h ago" if rest == 0 else f"{hours} h {rest:02d} ago"


def updated_at(document: dict[str, Any]) -> str:
    """Paris time of the forecast origin (``14:45``)."""
    return f"{datetime.fromisoformat(document['origin']).astimezone(PARIS):%H:%M}"


def live_status_html(document: dict[str, Any], status: str, age_s: float = 0.0) -> str:
    """The freshness pill of the header: live, or delayed with the age of the data."""
    if status == "stale":
        return (
            f'<div class="vf-live stale" role="status"><span class="vf-dot"></span>'
            f'<b>Delayed data</b><span class="vf-live-time">· updated {age_text(age_s)}</span>'
            "</div>"
        )
    return (
        f'<div class="vf-live" role="status"><span class="vf-dot"></span><b>Live</b>'
        f'<span class="vf-live-time">· Updated {updated_at(document)}</span></div>'
    )


def brand_html(href: str | None = None) -> str:
    """Logo tile and name; a link to ``href`` when given (the footer)."""
    logo = theme.image_url("logo")
    mark = f'<img src="{logo}" alt="" width="24" height="19">' if logo else icon("bike", 20, "#fff")
    inner = f'<span class="vf-brand-mark">{mark}</span><span class="vf-brand-name">VéloFlow</span>'
    if href:
        return f'<a class="vf-brand" href="{href}" aria-label="VéloFlow home">{inner}</a>'
    return f'<div class="vf-brand">{inner}</div>'


def page_head_html(title: str, subtitle: str, eyebrow: str | None = None) -> str:
    """Title block of a page: eyebrow, title, one line of context."""
    chip = f'<div class="vf-eyebrow">{escape(eyebrow)}</div>' if eyebrow else ""
    return (
        f'<header class="vf-head">{chip}<h1>{escape(title)}</h1><p>{escape(subtitle)}</p></header>'
    )


def stat_html(
    icon_name: str, tint: str, value: str, label: str, note: str = "", trend: str = ""
) -> str:
    """One key figure: icon tile, value, label and an optional note (``up``/``down``)."""
    extra = f'<div class="vf-stat-note {trend}">{escape(note)}</div>' if note else ""
    return (
        f'<div class="vf-stat"><span class="vf-icon-tile" style="background:{tint}14">'
        f'{icon(icon_name, 22, tint)}</span><div><div class="vf-stat-value">{escape(value)}'
        f'</div><div class="vf-stat-label">{escape(label)}</div>{extra}</div></div>'
    )


def stats_html(cells: list[str]) -> str:
    """Key figures side by side in one card."""
    return f'<div class="vf-stats">{"".join(cells)}</div>'


def count_cards_html(station: dict[str, Any], horizon: str) -> str:
    """Forecast of the three counts at a horizon, each with its 80 % interval."""
    forecast = station["forecasts"][horizon]
    cards = []
    for key, _, icon_name in COUNTS:
        low, high = forecast[f"{key}_interval"]
        capacity = max(1.0, float(station.get("capacity") or 0) or high or 1.0)
        cards.append(
            f'<div class="vf-count"><div class="vf-count-label">'
            f"{icon(icon_name, 16, COUNT_COLOURS[key])}{SHORT_LABELS[key]}</div>"
            f'<div class="vf-count-value">{forecast[key]:.0f}</div>'
            f'<div class="vf-count-note">likely {low:.0f}{DASH}{high:.0f}</div>'
            f"{_bar(forecast[key] / capacity, COUNT_COLOURS[key])}</div>"
        )
    return f'<div class="vf-counts">{"".join(cards)}</div>'


def risk_cards_html(station: dict[str, Any], horizon: str) -> str:
    """Probabilities that the station is empty, or full, at a horizon."""
    forecast = station["forecasts"][horizon]
    cards = "".join(
        f'<div class="vf-risk"><div class="vf-risk-top"><span class="vf-risk-value" '
        f'style="color:{TONES.get(risk_tone(p), theme.TEXT)}">{percent(p)}</span></div>'
        f'<div class="vf-risk-label">{label}</div>{_bar(p, risk_colour(p))}</div>'
        for label, p in (
            ("Station empty", forecast["p_empty"]),
            ("Station full", forecast["p_full"]),
        )
    )
    return f'<div class="vf-risks">{cards}</div>'


def station_head_html(station: dict[str, Any], extra: str = "", *, xl: bool = False) -> str:
    """Name and district of a station (``xl``: the title of the station page)."""
    size = 26 if xl else 20
    tag = "h1" if xl else "div"
    return (
        f'<div class="vf-station{" xl" if xl else ""}">'
        f'<span class="vf-icon-tile{" lg" if xl else ""}">{icon("pin", size, ACCENT)}</span><div>'
        f'<{tag} class="vf-station-name">{escape(station["name"])}</{tag}>'
        f'<div class="vf-station-sub">{district(station["code"])}{escape(extra)}</div></div></div>'
    )


def now_tiles_html(station: dict[str, Any]) -> str:
    """What the station holds now, with the change expected at the shortest horizon."""
    first = station_horizons(station)[0] if station["forecasts"] else None
    cells = []
    for key, label, icon_name in COUNTS:
        now = station["now"][key]
        later = station["forecasts"][first].get(key) if first else None
        note, trend = "", ""
        if later is not None:
            change = round(later - now)
            arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
            note = f"{arrow} {change:+d} in {first} min"
            trend = "up" if change > 0 else "down" if change < 0 else ""
        cells.append(stat_html(icon_name, COUNT_COLOURS[key], f"{now:.0f}", label, note, trend))
    return stats_html(cells)


def now_compact_html(station: dict[str, Any]) -> str:
    """What a station holds now, in three compact tiles (icon, number, label)."""
    tiles = "".join(
        f'<div class="vf-mini">{icon(icon_name, 18, COUNT_COLOURS[key])}'
        f'<div class="vf-mini-value">{station["now"][key]:.0f}</div>'
        f'<div class="vf-mini-label">{SHORT_LABELS[key]}</div></div>'
        for key, _, icon_name in COUNTS
    )
    return f'<div class="vf-minis">{tiles}</div>'


def horizon_table_html(station: dict[str, Any]) -> str:
    """Mechanical / e-bikes / docks forecast at every horizon of the station."""
    hs = station_horizons(station)
    cells = "".join(
        f'<div class="vf-hcell"><div class="vf-hcell-h">In {h} min</div><div class="vf-hcell-v">'
        f"{station['forecasts'][h]['mechanical']:.0f} / {station['forecasts'][h]['ebike']:.0f} / "
        f"{station['forecasts'][h]['docks']:.0f}</div></div>"
        for h in hs
    )
    return (
        f'<div class="vf-subtitle"><b>Next {" / ".join(hs)} min</b>'
        f'<small>mechanical / e-bikes / docks</small></div><div class="vf-hgrid">{cells}</div>'
    )


def tip_html(title: str, text: str, tone_name: str = "info") -> str:
    """A callout: ``info`` (violet), ``good``, ``fair`` or ``poor``."""
    mark = icon("check", 18, "#ffffff") if tone_name == "good" else icon("bulb", 18, "#ffffff")
    if tone_name == "poor":
        mark = icon("alert", 18, "#ffffff")
    return (
        f'<div class="vf-callout {tone_name}"><span class="vf-callout-icon">{mark}</span>'
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
        '<div class="vf-card-title">Across the network now</div><div class="vf-figures">'
        + "".join(
            f'<div class="vf-figure"><span class="vf-icon-tile" style="background:{tint}14">'
            f"{icon(name, 20, tint)}</span><div><b>{value}</b><small>{label}</small></div></div>"
            for name, tint, value, label in items
        )
        + "</div>"
    )


def empty_html(title: str, text: str) -> str:
    """An empty state: what is missing, and what to do."""
    return (
        f'<div class="vf-empty">{icon("info", 28, theme.TEXT_TERTIARY)}<b>{escape(title)}</b>'
        f"<span>{escape(text)}</span></div>"
    )


def risk_table_html(ranked: list[tuple[float, dict[str, Any]]], horizon: str, event: str) -> str:
    """The at-risk stations, most likely first."""
    if not ranked:
        empty = empty_html("All clear", f"No station is likely to be {event} right now.")
        return f'<div class="vf-card">{empty}</div>'
    rows = "".join(
        f'<tr><td class="vf-num">{i}</td><td>{escape(s["name"])}</td>'
        f'<td class="vf-muted">{district(s["code"])}</td><td class="r"><span class="vf-badge '
        f'{risk_tone(p)}">{percent(p)}</span></td></tr>'
        for i, (p, s) in enumerate(ranked, start=1)
    )
    return (
        f'<div class="vf-card"><div class="vf-card-title">Top {len(ranked)} at-risk stations'
        f'<small>{event.capitalize()} in {horizon} min</small></div><div class="vf-table-wrap">'
        '<table class="vf-table vf-rank"><thead><tr><th scope="col">#</th>'
        '<th scope="col">Station</th><th scope="col">District</th>'
        '<th scope="col" class="r">Chance</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></div>"
    )


def label_cells(rows: str, heads: list[str]) -> str:
    """Give every ``<td>`` of ``rows`` its column's name (``data-label``).

    On phones the style sheet shows each row as a card, every value beside the
    name of its column.
    """

    def label_row(row: re.Match[str]) -> str:
        names = iter(heads)
        return re.sub(
            r"<td(?=[ >])",
            lambda _: f'<td data-label="{escape(next(names, ""), quote=True)}"',
            row.group(0),
        )

    return re.sub(r"<tr>.*?</tr>", label_row, rows, flags=re.S)


def options_html(options: list[dict[str, Any]], need: Need, *, grid: bool = False) -> str:
    """Stations within reach, nearest first, with the chance of success."""
    if not options:
        return empty_html(
            "No station within this walking distance", "Choose a longer walking time."
        )
    thing = "bike" if need == "bike" else "dock"
    rows = "".join(
        f'<div class="vf-list-item"><div style="min-width:0"><div class="vf-list-name">'
        f'{escape(o["station"]["name"])}</div><div class="vf-list-meta">{o["distance_m"]:.0f} m'
        f" · ~{walk_minutes(o['distance_m'])} min walk</div></div>"
        f'<span class="vf-badge {tone(o["probability"])}">{percent(o["probability"])} {thing}'
        "</span></div>"
        for o in options[:8]
    )
    return f'<div class="vf-list{" vf-list-grid" if grid else ""}">{rows}</div>'


def legend_html(labels: tuple[str, str, str], extra: tuple[tuple[str, str], ...] = ()) -> str:
    """Colour key of a chance map; ``extra`` adds (colour, label) entries."""
    swatches = (hex_colour(GREEN), hex_colour(ORANGE), hex_colour(RED))
    entries = (*zip(swatches, labels, strict=True), *extra)
    return (
        '<div class="vf-legend">'
        + "".join(
            f'<span><i style="background:{c}"></i>{escape(label)}</span>' for c, label in entries
        )
        + "</div>"
    )


def _when(stamp: Any) -> str:
    """A UTC timestamp of the model card for people (``10 Nov 2026 23:59 UTC``)."""
    moment = datetime.fromisoformat(str(stamp))
    return f"{moment:%d %b %Y %H:%M} UTC"


def series(items: list[str]) -> str:
    """``15, 30 and 60`` from a list of words."""
    return items[0] if len(items) < 2 else f"{', '.join(items[:-1])} and {items[-1]}"


def about_html(document: dict[str, Any]) -> str:
    """The About text, every figure read from the forecast document.

    Fields missing from older documents are left out rather than guessed.
    """
    stations = document["stations"]
    card = document["model"]
    span = f"up to {_when(card['data_end'])}"
    if card.get("data_start"):
        span = f"from {_when(card['data_start'])} to {_when(card['data_end'])}"
    examples = f" on {card['examples']:,} examples" if card.get("examples") else ""
    interval = (
        f" Each count comes with a {card['interval_level']:.0%} prediction interval "
        "(split conformal), shown as its likely range."
        if card.get("interval_level")
        else ""
    )
    return (
        '<div class="vf-card"><div class="vf-prose"><p>VéloFlow forecasts, for each of the '
        f"{len(stations):,} Vélib' Métropole stations of this update, the number of mechanical "
        f"bikes, e-bikes and free docks {series(horizons(stations))} minutes ahead, and the "
        "probability that the station will be empty or full.</p>"
        "<h3>Data</h3><p>The public Vélib' feed, cleaned by explicit quality rules (stale "
        "versions, closed stations, frozen feed); Open-Meteo weather forecasts; the French "
        f"school calendar. The model in use was trained{examples} {span}.</p>"
        f"<h3>Model</h3><p>The model <code>{escape(str(card['name']))}</code> is one global "
        "model per horizon, trained on every station at once. It forecasts the change from "
        f"now, and calibrated probabilities of empty and full.{interval} Every model is "
        "evaluated by the same backtest, which fails if a forecast reads data from after "
        "its own time.</p>"
        "<h3>Accuracy</h3><p>Measured by that backtest against simple baselines, and re-run "
        "as the history grows; the figures are in the evaluation report, not repeated here "
        "so that they never go stale.</p>"
        f'<p><a href="{GITHUB}">Source code and evaluation on GitHub</a></p></div></div>'
    )


GITHUB = "https://github.com/HamzaOuba00-1/veloflow"
