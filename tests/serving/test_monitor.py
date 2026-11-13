"""Tests for the monitoring of the live service (real accuracy, collector, models)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from veloflow.models.artifact import ModelCard
from veloflow.serving.monitor import (
    COVERAGE_TOLERANCE,
    KEEP_FORECASTS_MIN,
    MIN_PAIRS,
    Monitor,
    alerts,
    collector_health,
    expected_fetches,
    live_accuracy,
    score_pairs,
)
from veloflow.storage.compaction import STATUS_SCHEMA

ORIGIN = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)
CARD = ModelCard("lightgbm", "2026-11-12T00:00:00", "2026-11-05T00:00:00", "2026-11-11", 10, "0.1")


def forecasts(origin: datetime = ORIGIN, horizon: int = 15, **values: float) -> pl.DataFrame:
    """Forecasts of two stations: state now (5, 0, 10), predictions as given."""
    row: dict[str, Any] = {
        "origin": origin,
        "horizon_min": horizon,
        "target_at": origin + timedelta(minutes=horizon),
        "mechanical": 5,
        "ebike": 0,
        "docks": 10,
        "pred_mechanical": 4.0,
        "pred_ebike": 0.0,
        "pred_docks": 11.0,
        "lo_mechanical": 3,
        "lo_ebike": 0,
        "lo_docks": 10,
        "hi_mechanical": 5,
        "hi_ebike": 1,
        "hi_docks": 12,
        "p_empty": 0.1,
        "p_full": 0.0,
        **values,
    }
    return pl.DataFrame([{**row, "station_id": 1}, {**row, "station_id": 2}]).with_columns(
        pl.col("origin", "target_at").dt.cast_time_unit("us"),
        pl.col("horizon_min").cast(pl.Int16),
    )


def states(at: datetime, mechanical: int, docks: int = 11) -> pl.DataFrame:
    """Both stations seen at ``at`` (and 5 minutes before), with the given counts."""
    rows = [
        {
            "feed_updated_at": t,
            "fetched_at": t,
            "station_id": s,
            "mechanical": mechanical,
            "ebike": 0,
            "docks": docks,
            "is_installed": True,
            "is_renting": True,
            "is_returning": True,
            "last_reported": t,
            "operational": True,
        }
        for t in (at - timedelta(minutes=5), at)
        for s in (1, 2)
    ]
    return pl.DataFrame(rows, schema={**STATUS_SCHEMA, "operational": pl.Boolean})


def test_score_pairs_sums_errors_of_the_model_and_of_persistence() -> None:
    pairs = forecasts().with_columns(
        mechanical_actual=pl.Series([4, 0], dtype=pl.Int16),
        ebike_actual=pl.lit(0, dtype=pl.Int16),
        docks_actual=pl.Series([11, 15], dtype=pl.Int16),
    )

    (row,) = score_pairs(pairs).iter_rows(named=True)

    assert row["n"] == 2
    assert row["ae_model_mechanical"] == pytest.approx(0 + 4)  # predicted 4: |4-4| + |4-0|
    assert row["ae_persistence_mechanical"] == pytest.approx(1 + 5)  # now 5
    assert row["covered_mechanical"] == 1  # 4 in [3, 5], 0 is not
    assert row["covered_docks"] == 1  # 11 in [10, 12], 15 is not
    assert row["events_empty"] == 1  # station 2 has no bike left
    assert row["se_model_empty"] == pytest.approx(0.1**2 + 0.9**2)
    assert row["se_persistence_empty"] == pytest.approx(0 + 1)  # "not empty" twice


def test_forecasts_are_scored_once_when_their_time_comes(tmp_path: Path) -> None:
    monitor = Monitor(tmp_path)
    monitor.keep(forecasts(horizon=15))
    monitor.keep(forecasts(horizon=60).with_columns(pl.col("origin") + timedelta(minutes=10)))

    early = monitor.score(states(ORIGIN + timedelta(minutes=10), mechanical=4), ORIGIN)
    assert early.height == 0  # 08:15 not reached yet

    now = ORIGIN + timedelta(minutes=16)
    scores = monitor.score(states(ORIGIN + timedelta(minutes=15), mechanical=4), now)
    again = monitor.score(states(ORIGIN + timedelta(minutes=15), mechanical=4), now)

    assert scores.select("origin", "horizon_min", "n").rows() == [(ORIGIN, 15, 2)]
    assert again.equals(scores)  # read back from disk, not scored twice
    live = live_accuracy(scores, now, level=0.8)
    figures = live["horizons"]["15"]["targets"]["mechanical"]
    assert figures == {"mae": 0.0, "mae_persistence": 1.0, "coverage": 1.0}
    assert live["since"] == ORIGIN.isoformat(timespec="seconds")


def test_old_forecasts_and_scores_are_dropped(tmp_path: Path) -> None:
    monitor = Monitor(tmp_path)
    monitor.keep(forecasts())
    monitor.keep(pl.DataFrame())  # nothing to keep: nothing written
    later = ORIGIN + timedelta(minutes=KEEP_FORECASTS_MIN + 1)

    assert monitor.score(states(later, mechanical=4), later).height == 0
    assert list((tmp_path / "forecasts").glob("*.parquet")) == []


def test_models_in_service_are_recorded_once_each(tmp_path: Path) -> None:
    monitor = Monitor(tmp_path)

    assert len(monitor.models(CARD, ORIGIN)) == 1
    assert len(monitor.models(CARD, ORIGIN + timedelta(minutes=10))) == 1
    newer = replace(CARD, trained_at="2026-11-13T00:00:00")
    history = monitor.models(newer, ORIGIN + timedelta(hours=1))

    assert [m["trained_at"] for m in history] == [newer.trained_at, CARD.trained_at]
    assert history[0]["in_service_from"] == "2026-11-13T09:00:00+00:00"


def test_collector_health_counts_the_last_hour() -> None:
    fetches = pl.DataFrame(
        {
            "fetched_at": [ORIGIN - timedelta(minutes=m) for m in (90, 30, 10, 1)],
            "status": ["ok", "ok", "degraded", "ok"],
        }
    ).with_columns(pl.col("fetched_at").dt.cast_time_unit("us"))
    status = states(ORIGIN, mechanical=3)

    health = collector_health(fetches, status, ORIGIN)

    assert health == {
        "last_fetch": (ORIGIN - timedelta(minutes=1)).isoformat(timespec="seconds"),
        "fetches_last_hour": 3,
        "fetches_ok_last_hour": 2,
        "expected_last_hour": expected_fetches(60),
        "stations": 2,
        "stations_operational": 2,
    }
    assert expected_fetches(60) == 120  # every 30 seconds
    empty = collector_health(fetches.head(0), status.head(0), ORIGIN)
    assert empty["last_fetch"] is None
    assert empty["stations_operational"] == 0


def live(mae: float, persistence: float, coverage: float, pairs: int = MIN_PAIRS) -> dict[str, Any]:
    figures = {"mae": mae, "mae_persistence": persistence, "coverage": coverage}
    return {"horizons": {"15": {"pairs": pairs, "targets": {"mechanical": figures}}}}


def test_alerts_follow_the_rules() -> None:
    fresh = {"last_fetch": ORIGIN.isoformat()}
    weather = ORIGIN - timedelta(hours=1)

    assert alerts(fresh, weather, live(0.3, 0.4, 0.8), 0.8, ORIGIN) == []
    assert alerts(fresh, weather, live(0.5, 0.4, 0.5, pairs=10), 0.8, ORIGIN) == []  # too few

    found = alerts(
        {"last_fetch": None},
        None,
        live(0.5, 0.4, 0.8 + COVERAGE_TOLERANCE + 0.01),
        0.8,
        ORIGIN,
    )
    assert [a["rule"] for a in found] == ["collector", "weather", "accuracy", "calibration"]
    assert found[2]["message"] == "mechanical at 15 min: worse than persistence"
    assert found[3]["message"] == "mechanical at 15 min: intervals cover 86% instead of 80%"


def test_update_returns_the_status_block(tmp_path: Path) -> None:
    fetches = pl.DataFrame(
        {"fetched_at": [ORIGIN], "status": ["ok"]},
        schema={"fetched_at": pl.Datetime("us", "UTC"), "status": pl.String},
    )

    block = Monitor(tmp_path).update(
        forecasts(), states(ORIGIN, 5), fetches, CARD, 0.8, ORIGIN, ORIGIN
    )

    assert set(block) == {"collector", "weather", "live", "models", "alerts", "rules"}
    assert block["live"]["horizons"] == {}  # nothing scored on the first run
    assert block["rules"]["min_pairs"] == MIN_PAIRS
    assert len(list((tmp_path / "forecasts").glob("*.parquet"))) == 1
