"""HTML parts of the Admin page (requirement F6), from the document's ``status`` block.

Every figure is read from the block that ``veloflow-predict`` measures on the
VM (``serving.monitor``); thresholds come from the block's own ``rules``.
Pure functions, tested without a browser.
"""

from datetime import datetime
from html import escape
from typing import Any

from veloflow.serving import theme
from veloflow.serving.ui import DASH, TONES, stat_html, stats_html, tip_html

TARGET_LABELS = {"mechanical": "Mechanical bikes", "ebike": "E-bikes", "docks": "Free docks"}


def _ago(stamp: str | None, now: datetime) -> str:
    """``3 min ago`` from an ISO time; a dash when unknown."""
    if stamp is None:
        return DASH
    minutes = max(0, round((now - datetime.fromisoformat(stamp)).total_seconds() / 60))
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def _num(value: float | None, digits: int = 2) -> str:
    return DASH if value is None else f"{value:.{digits}f}"


def _pct(value: float | None) -> str:
    return DASH if value is None else f"{value:.0%}"


def _day(stamp: str | None) -> str:
    return DASH if not stamp else f"{datetime.fromisoformat(stamp):%d %b %Y %H:%M}"


def _table_card(title: str, note: str, heads: list[str], rows: str, caption: str = "") -> str:
    """A table in a card; a heading starting with ``#`` is a number column (right-aligned)."""
    cells = "".join(
        f'<th scope="col" class="r">{escape(h[1:])}</th>'
        if h.startswith("#")
        else f'<th scope="col">{escape(h)}</th>'
        for h in heads
    )
    extra = f'<p class="vf-caption">{escape(caption)}</p>' if caption else ""
    return (
        f'<div class="vf-card"><div class="vf-card-title">{escape(title)}'
        f'<small>{escape(note)}</small></div><div class="vf-table-wrap"><table class="vf-table">'
        f"<thead><tr>{cells}</tr></thead><tbody>{rows}</tbody></table></div>{extra}</div>"
    )


def alerts_html(status: dict[str, Any]) -> str:
    """Alerts raised by the monitoring rules, or a line saying all checks pass."""
    found = status["alerts"]
    if not found:
        return tip_html(
            "All checks pass",
            "Collector, weather, accuracy and calibration are within their rules.",
            "good",
        )
    items = "".join(f"<li>{escape(a['message'])}</li>" for a in found)
    title = f"{len(found)} alert{'s' if len(found) > 1 else ''}"
    return tip_html(title, "", "poor").replace("<p></p>", f"<ul>{items}</ul>")


def health_html(status: dict[str, Any], now: datetime) -> str:
    """Collector, stations and weather, as key figures."""
    c = status["collector"]
    return stats_html(
        [
            stat_html("clock", theme.PRIMARY, _ago(c["last_fetch"], now), "Last fetch of the feed"),
            stat_html(
                "chart",
                theme.COUNT_COLOURS["ebike"],
                f"{c['fetches_ok_last_hour']} / {c['expected_last_hour']}",
                "Usable fetches, last hour",
                f"{c['fetches_last_hour']} archived",
            ),
            stat_html(
                "pin",
                theme.COUNT_COLOURS["docks"],
                f"{c['stations_operational']:,} / {c['stations']:,}",
                "Stations in service",
            ),
            stat_html(
                "info",
                theme.WARNING,
                _ago(status["weather"]["fetched_at"], now),
                "Weather snapshot",
            ),
        ]
    )


def accuracy_html(status: dict[str, Any]) -> str:
    """Real accuracy per horizon and count, against persistence, with interval coverage."""
    live = status["live"]
    rules = status["rules"]
    if not live["horizons"]:
        return (
            '<div class="vf-card"><div class="vf-empty"><b>No forecast scored yet</b><span>'
            "The first ones are scored when their target time comes (15 minutes after the "
            "first run).</span></div></div>"
        )
    rows = []
    for horizon, figures in live["horizons"].items():
        judged = figures["pairs"] >= rules["min_pairs"]
        for target, t in figures["targets"].items():
            gain = (
                1 - t["mae"] / t["mae_persistence"]
                if t["mae"] is not None and t["mae_persistence"]
                else None
            )
            tint = TONES["good"] if gain is not None and gain > 0 else TONES["poor"]
            rows.append(
                f"<tr><td>{horizon} min</td><td>{TARGET_LABELS.get(target, escape(target))}</td>"
                f'<td class="r vf-fig">{_num(t["mae"])}</td>'
                f'<td class="r vf-fig">{_num(t["mae_persistence"])}</td>'
                f'<td class="r vf-fig" style="color:{tint if judged else "inherit"}">'
                f"<b>{DASH if gain is None else f'{gain:+.0%}'}</b></td>"
                f'<td class="r vf-fig">{_pct(t["coverage"])}</td>'
                f'<td class="r vf-fig vf-muted">{figures["pairs"]:,}</td></tr>'
            )
    return _table_card(
        f"Real accuracy, last {rules['live_window_hours']} h",
        f"since {_day(live['since'])} UTC",
        [
            "Horizon",
            "Count",
            "#MAE model",
            "#MAE persistence",
            "#Gain",
            f"#In {live['interval_level']:.0%} interval",
            "#Forecasts",
        ],
        "".join(rows),
        f"Persistence forecasts that nothing changes. A horizon is judged from "
        f"{rules['min_pairs']:,} forecasts; intervals should cover "
        f"{live['interval_level']:.0%} {chr(177)} {rules['coverage_tolerance']:.0%}.",
    )


def events_html(status: dict[str, Any]) -> str:
    """Brier score of the empty and full probabilities, against persistence."""
    live = status["live"]
    if not live["horizons"]:
        return ""
    rows = "".join(
        f"<tr><td>{h} min</td><td>{label}</td>"
        f'<td class="r vf-fig">{_num(f[event]["brier"], 3)}</td>'
        f'<td class="r vf-fig">{_num(f[event]["brier_persistence"], 3)}</td>'
        f'<td class="r vf-fig">{_pct(f[event]["rate"])}</td></tr>'
        for h, f in live["horizons"].items()
        for event, label in (("empty", "No bike"), ("full", "No dock"))
    )
    return _table_card(
        "Empty and full stations",
        "",
        ["Horizon", "Event", "#Brier model", "#Brier persistence", "#Observed rate"],
        rows,
        "Brier score: mean squared error of the probability (lower is better).",
    )


def models_html(status: dict[str, Any]) -> str:
    """Models put in service on the VM, newest first."""
    rows = "".join(
        f"<tr><td><code>{escape(str(m['name']))}</code></td><td>{_day(m['trained_at'])}</td>"
        f"<td>{_day(m['data_start'])} {DASH} {_day(m['data_end'])}</td>"
        f'<td class="r vf-fig">{m["examples"]:,}</td><td>{_day(m["in_service_from"])}</td>'
        f"<td>{escape(str(m['package_version']))}</td></tr>"
        for m in status["models"]
    )
    return _table_card(
        "Models in service",
        "",
        ["Model", "Trained", "Data", "#Examples", "In service from", "Code"],
        rows,
    )
