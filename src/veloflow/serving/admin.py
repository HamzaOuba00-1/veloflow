"""HTML parts of the Admin page (requirement F6), from the document's ``status`` block.

Every figure is read from the block that ``veloflow-predict`` measures on the
VM (``serving.monitor``); thresholds come from the block's own ``rules``.
Pure functions, tested without a browser.
"""

from datetime import datetime
from html import escape
from typing import Any

from veloflow.serving.ui import DASH, TONES, icon, kpi_html, tip_html

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
    return (
        f'<div class="vf-card vf-tip poor"><span class="vf-tip-icon">'
        f"{icon('alert', 22, '#ffffff')}</span><div><b>{len(found)} alert"
        f'{"s" if len(found) > 1 else ""}</b><ul class="vf-alerts">{items}</ul></div></div>'
    )


def health_html(status: dict[str, Any], now: datetime) -> str:
    """Collector, stations and weather, as key figures."""
    c = status["collector"]
    return (
        '<div class="vf-kpis">'
        + kpi_html("clock", "#6366f1", _ago(c["last_fetch"], now), "Last fetch of the feed")
        + kpi_html(
            "chart",
            "#0ea5e9",
            f"{c['fetches_ok_last_hour']} / {c['expected_last_hour']}",
            "Usable fetches, last hour",
            f"{c['fetches_last_hour']} archived",
        )
        + kpi_html(
            "pin",
            "#22c55e",
            f"{c['stations_operational']:,} / {c['stations']:,}",
            "Stations in service",
        )
        + kpi_html(
            "info", "#f59e0b", _ago(status["weather"]["fetched_at"], now), "Weather snapshot"
        )
        + "</div>"
    )


def accuracy_html(status: dict[str, Any]) -> str:
    """Real accuracy per horizon and count, against persistence, with interval coverage."""
    live = status["live"]
    rules = status["rules"]
    if not live["horizons"]:
        return (
            '<div class="vf-card vf-muted">No forecast scored yet: the first ones are scored '
            "when their target time comes (15 minutes after the first run).</div>"
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
                f'<td class="vf-fig">{_num(t["mae"])}</td>'
                f'<td class="vf-fig">{_num(t["mae_persistence"])}</td>'
                f'<td class="vf-fig" style="color:{tint if judged else "inherit"}">'
                f"<b>{DASH if gain is None else f'{gain:+.0%}'}</b></td>"
                f'<td class="vf-fig">{_pct(t["coverage"])}</td>'
                f'<td class="vf-fig vf-muted">{figures["pairs"]:,}</td></tr>'
            )
    return (
        f'<div class="vf-card vf-table-card"><div class="vf-section-title">{icon("chart", 18)} '
        f"Real accuracy, last {rules['live_window_hours']} h (since {_day(live['since'])} UTC)"
        '</div><table class="vf-table"><thead><tr><th>Horizon</th><th>Count</th>'
        "<th>MAE model</th><th>MAE persistence</th><th>Gain</th>"
        f"<th>In {live['interval_level']:.0%} interval</th><th>Forecasts</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f'<p class="vf-muted">Persistence forecasts that nothing changes. A horizon is judged '
        f"from {rules['min_pairs']:,} forecasts; intervals should cover "
        f"{live['interval_level']:.0%} {chr(177)} {rules['coverage_tolerance']:.0%}.</p></div>"
    )


def events_html(status: dict[str, Any]) -> str:
    """Brier score of the empty and full probabilities, against persistence."""
    live = status["live"]
    if not live["horizons"]:
        return ""
    rows = "".join(
        f"<tr><td>{h} min</td><td>{label}</td>"
        f'<td class="vf-fig">{_num(f[event]["brier"], 3)}</td>'
        f'<td class="vf-fig">{_num(f[event]["brier_persistence"], 3)}</td>'
        f'<td class="vf-fig">{_pct(f[event]["rate"])}</td></tr>'
        for h, f in live["horizons"].items()
        for event, label in (("empty", "No bike"), ("full", "No dock"))
    )
    return (
        f'<div class="vf-card vf-table-card"><div class="vf-section-title">{icon("alert", 18)} '
        'Empty and full stations</div><table class="vf-table"><thead><tr><th>Horizon</th>'
        "<th>Event</th><th>Brier model</th><th>Brier persistence</th><th>Observed rate</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        '<p class="vf-muted">Brier score: mean squared error of the probability (lower is '
        "better).</p></div>"
    )


def models_html(status: dict[str, Any]) -> str:
    """Models put in service on the VM, newest first."""
    rows = "".join(
        f"<tr><td><code>{escape(str(m['name']))}</code></td><td>{_day(m['trained_at'])}</td>"
        f"<td>{_day(m['data_start'])} {DASH} {_day(m['data_end'])}</td>"
        f'<td class="vf-fig">{m["examples"]:,}</td><td>{_day(m["in_service_from"])}</td>'
        f"<td>{escape(str(m['package_version']))}</td></tr>"
        for m in status["models"]
    )
    return (
        f'<div class="vf-card vf-table-card"><div class="vf-section-title">{icon("bulb", 18)} '
        'Models in service</div><table class="vf-table"><thead><tr><th>Model</th>'
        "<th>Trained</th><th>Data</th><th>Examples</th><th>In service from</th><th>Code</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
