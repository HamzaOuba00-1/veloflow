"""Tests for the Admin page parts (requirement F6)."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from veloflow.serving.admin import (
    accuracy_html,
    alerts_html,
    events_html,
    health_html,
    models_html,
)
from veloflow.serving.ui import DASH

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)  # when the block was measured
Block = Callable[..., dict[str, Any]]


def test_alerts(status_block: Block) -> None:
    assert "All checks pass" in alerts_html(status_block())
    two = alerts_html(status_block(alerts=[{"message": "a <b>"}, {"message": "c"}]))
    assert "2 alerts" in two
    assert "a &lt;b&gt;" in two


def test_health_figures_come_from_the_block(status_block: Block) -> None:
    html = health_html(status_block(), NOW)

    assert "3 min ago" in html
    assert "118 / 120" in html
    assert "121 archived" in html
    assert "1,460 / 1,496" in html
    assert DASH in html  # no weather snapshot known


def test_accuracy_table(status_block: Block) -> None:
    html = accuracy_html(status_block())

    assert "Real accuracy, last 24 h (since 12 Nov 2026 22:30 UTC)" in html
    assert "+25%" in html  # 1 - 0.3 / 0.4
    assert "-25%" in html  # 1 - 0.5 / 0.4
    assert "83%" in html
    assert "50,000" in html
    assert "In 80% interval" in html
    assert "judged from 1,000 forecasts" in html
    empty = status_block(live={"since": None, "interval_level": 0.8, "horizons": {}})
    assert "No forecast scored yet" in accuracy_html(empty)
    assert events_html(empty) == ""


def test_events_and_models(status_block: Block) -> None:
    events = events_html(status_block())
    assert "0.051" in events
    assert "6%" in events

    models = models_html(status_block())
    assert "11 Nov 2026 23:46" in models
    assert "05 Nov 2026 00:00" in models
    assert "1,234,567" in models
