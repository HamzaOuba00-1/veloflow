"""Tests for the LightGBM forecaster, on the synthetic status (small models)."""

from datetime import UTC, date, datetime

import numpy as np
import polars as pl
import pytest

from veloflow.evaluation.backtest import check_no_lookahead, day_bounds, run_backtest
from veloflow.features.build import make_context
from veloflow.models.base import INTERVAL_COLUMNS, KEYS, PREDICTION_COLUMNS, TrainingData
from veloflow.models.gbm import (
    GradientBoosting,
    conformal_half_width,
    daily_profile_sums,
    profile_features,
)

STATIONS = pl.DataFrame(
    {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
    schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
)
SMALL: dict[str, object] = {
    "n_estimators": 10,
    "num_leaves": 7,
    "min_child_samples": 5,
    "verbose": -1,
}


def model() -> GradientBoosting:
    return GradientBoosting(make_context(STATIONS), params=SMALL)


def no_masks(history: pl.DataFrame, cutoff: datetime) -> TrainingData:
    return TrainingData(
        history.filter(pl.col("fetched_at") < cutoff),
        pl.DataFrame(schema={"slot_start": pl.Datetime("us", "UTC")}),
        pl.DataFrame(
            schema={
                "station_id": pl.Int64,
                "frozen_from": pl.Datetime("us", "UTC"),
                "frozen_until": pl.Datetime("us", "UTC"),
            }
        ),
        cutoff,
    )


def test_backtest_runs_and_passes_the_lookahead_check(synthetic_status: pl.DataFrame) -> None:
    result = run_backtest(model(), synthetic_status)

    frame = result.frame
    assert frame.height == sum(fold.kept for fold in result.folds) > 0
    assert frame.select(PREDICTION_COLUMNS).null_count().sum_horizontal().item() == 0
    assert frame.filter(~pl.col("p_empty").is_between(0, 1)).height == 0
    # Every forecast sits inside its interval, and intervals stay within [0, cap].
    for name in ("mechanical", "ebike", "docks"):
        assert (
            frame.filter(
                (pl.col(f"lo_{name}") > pl.col(f"pred_{name}").round())
                | (pl.col(f"hi_{name}") < pl.col(f"pred_{name}").round())
                | (pl.col(f"lo_{name}") < 0)
            ).height
            == 0
        )
    # Counts never go below zero.
    assert (
        frame.select(pl.min_horizontal("pred_mechanical", "pred_ebike", "pred_docks").min()).item()
        >= 0
    )


def test_an_event_never_seen_in_training_gets_its_frequency(
    synthetic_status: pl.DataFrame,
) -> None:
    never_full = synthetic_status.with_columns(pl.col("docks") + 1)
    cutoff = day_bounds(date(2026, 11, 10))[0]
    forecaster = model()
    forecaster.fit(no_masks(never_full, cutoff))

    examples = (
        never_full.filter(pl.col("fetched_at") >= cutoff)
        .head(20)
        .select(
            "station_id",
            pl.col("fetched_at").alias("origin"),
            pl.lit(15, dtype=pl.Int16).alias("horizon_min"),
            (pl.col("fetched_at") + pl.duration(minutes=15)).alias("target_at"),
            "mechanical",
            "ebike",
            "docks",
        )
    )
    predictions = forecaster.predict(never_full, examples)

    assert predictions.columns == [*KEYS, *PREDICTION_COLUMNS, *INTERVAL_COLUMNS]
    assert predictions["p_full"].unique().to_list() == [0.0]
    check_no_lookahead(forecaster, never_full, examples, examples["origin"][10])


def test_predict_before_fit_fails() -> None:
    with pytest.raises(RuntimeError, match="fit"):
        model().predict(pl.DataFrame(), pl.DataFrame())


def test_training_profile_leaves_the_own_day_out() -> None:
    # Station 1, Monday 13:00 Paris: 2 bikes on 9 November, 6 on 16 November.
    states = pl.DataFrame(
        {
            "station_id": [1, 1],
            "time": [datetime(2026, 11, 9, 12, tzinfo=UTC), datetime(2026, 11, 16, 12, tzinfo=UTC)],
            "mechanical": [2, 6],
            "ebike": [0, 0],
            "docks": [8, 4],
        },
        schema_overrides={"time": pl.Datetime("us", "UTC")},
    )
    sums = daily_profile_sums(states)
    frame = pl.DataFrame(
        {
            "station_id": [1, 1],
            "t": [
                datetime(2026, 11, 9, 12, 5, tzinfo=UTC),
                datetime(2026, 11, 23, 12, 5, tzinfo=UTC),
            ],
        },
        schema_overrides={"t": pl.Datetime("us", "UTC")},
    )

    training = profile_features(frame, sums, "t", "p", leave_own_day_out=True)
    serving = profile_features(frame, sums, "t", "p", leave_own_day_out=False)

    # 9 November sees only the 16th; 23 November (no data that day) sees both.
    assert training["p_profile_mechanical"].to_list() == [6.0, 4.0]
    assert serving["p_profile_mechanical"].to_list() == [4.0, 4.0]


def test_a_slot_seen_only_on_its_own_day_is_unknown() -> None:
    states = pl.DataFrame(
        {
            "station_id": [1],
            "time": [datetime(2026, 11, 9, 12, tzinfo=UTC)],
            "mechanical": [2],
            "ebike": [0],
            "docks": [8],
        },
        schema_overrides={"time": pl.Datetime("us", "UTC")},
    )
    frame = pl.DataFrame(
        {"station_id": [1], "t": [datetime(2026, 11, 9, 12, 5, tzinfo=UTC)]},
        schema_overrides={"t": pl.Datetime("us", "UTC")},
    )

    training = profile_features(frame, daily_profile_sums(states), "t", "p", leave_own_day_out=True)

    assert training["p_profile_mechanical"].to_list() == [None]


def examples_of(status: pl.DataFrame, start: datetime, horizon: int, rows: int) -> pl.DataFrame:
    return (
        status.filter(pl.col("fetched_at") >= start)
        .head(rows)
        .select(
            "station_id",
            pl.col("fetched_at").alias("origin"),
            pl.lit(horizon, dtype=pl.Int16).alias("horizon_min"),
            (pl.col("fetched_at") + pl.duration(minutes=horizon)).alias("target_at"),
            "mechanical",
            "ebike",
            "docks",
        )
    )


def test_training_budget_and_default_settings(synthetic_status: pl.DataFrame) -> None:
    cutoff = day_bounds(date(2026, 11, 10))[0]
    forecaster = GradientBoosting(make_context(STATIONS), max_rows_per_horizon=200)
    forecaster.fit(no_masks(synthetic_status, cutoff))

    assert forecaster.params["n_estimators"] == 300
    assert (
        forecaster.predict(synthetic_status, examples_of(synthetic_status, cutoff, 30, 0)).height
        == 0
    )


def test_a_horizon_without_training_examples_cannot_be_forecast(
    synthetic_status: pl.DataFrame,
) -> None:
    # 40 minutes of history: examples exist at 15 and 30 minutes, not at 60.
    start = synthetic_status["fetched_at"].min()
    short = synthetic_status.filter(pl.col("fetched_at") < start + pl.duration(minutes=40))
    cutoff = datetime(2026, 11, 8, 12, 40, tzinfo=UTC)
    forecaster = model()
    forecaster.fit(no_masks(short, cutoff))

    assert sorted(forecaster.models) == [15, 30]
    with pytest.raises(RuntimeError, match="no model for horizon 60"):
        forecaster.predict(synthetic_status, examples_of(synthetic_status, cutoff, 60, 4))


def test_conformal_half_width_is_finite_sample_corrected() -> None:
    errors = np.array([0, 0, 0, 0, 0, 0, 1, 1, -2, 3], dtype=float)

    # n = 10: rank ceil(11 x 0.8) = 9, the 9th smallest |error| = 2.
    assert conformal_half_width(errors) == 2.0
    # n = 9 at 70 %: rank 7, the 7th smallest |error| = 1.
    assert conformal_half_width(errors[:9], level=0.7) == 1.0
    # Too few examples for the level: no finite guarantee.
    assert conformal_half_width(np.array([1.0, 2.0])) == float("inf")
