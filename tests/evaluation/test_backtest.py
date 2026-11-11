"""Tests for the rolling-origin backtest bench."""

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from veloflow.evaluation.backtest import (
    InvalidPredictionError,
    LookaheadError,
    check_no_lookahead,
    day_bounds,
    evaluation_days,
    run_backtest,
    validate_predictions,
)
from veloflow.evaluation.metrics import regression_metrics
from veloflow.models.base import KEYS, TrainingData


class Persistence:
    """Forecasts that nothing changes; records what ``fit`` received."""

    name = "persistence"

    def __init__(self) -> None:
        self.seen: list[tuple[datetime, datetime | None]] = []

    def fit(self, data: TrainingData) -> None:
        self.seen.append((data.cutoff, data.history["fetched_at"].max()))  # type: ignore[arg-type]

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        bikes = pl.col("mechanical") + pl.col("ebike")
        return examples.select(
            *KEYS,
            pred_mechanical=pl.col("mechanical").cast(pl.Float64),
            pred_ebike=pl.col("ebike").cast(pl.Float64),
            pred_docks=pl.col("docks").cast(pl.Float64),
            p_empty=(bikes == 0).cast(pl.Float64),
            p_full=(pl.col("docks") == 0).cast(pl.Float64),
        )


class Clairvoyant(Persistence):
    """Cheats: forecasts the last state of the history it is given."""

    name = "clairvoyant"

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        last = (
            history.sort("fetched_at")
            .group_by("station_id")
            .agg(pl.col("mechanical", "ebike", "docks").last())
        )
        return super().predict(
            history,
            examples.drop("mechanical", "ebike", "docks").join(last, on="station_id", how="left"),
        )


def test_day_bounds_follow_paris_clock_changes() -> None:
    start, end = day_bounds(date(2026, 11, 9))
    assert (start, end) == (
        datetime(2026, 11, 8, 23, tzinfo=UTC),
        datetime(2026, 11, 9, 23, tzinfo=UTC),
    )
    start, end = day_bounds(date(2027, 3, 28))  # spring forward
    assert end - start == timedelta(hours=23)


def test_evaluation_days_are_complete_and_have_a_past(synthetic_status: pl.DataFrame) -> None:
    # Day 8 starts at 13:00 Paris: partial, and nothing before it.
    assert evaluation_days(synthetic_status) == [date(2026, 11, 9), date(2026, 11, 10)]
    assert evaluation_days(synthetic_status.head(0)) == []


def test_days_the_collector_missed_are_not_test_days(synthetic_status: pl.DataFrame) -> None:
    start, end = day_bounds(date(2026, 11, 9))
    missed_day = synthetic_status.filter(~pl.col("fetched_at").is_between(start, end))
    half_day = synthetic_status.filter(
        ~pl.col("fetched_at").is_between(start + timedelta(hours=12), end)
    )

    assert evaluation_days(missed_day) == [date(2026, 11, 10)]
    assert evaluation_days(half_day) == [date(2026, 11, 10)]
    assert evaluation_days(half_day, min_coverage=0.5) == [date(2026, 11, 9), date(2026, 11, 10)]


def test_persistence_end_to_end(synthetic_status: pl.DataFrame) -> None:
    model = Persistence()

    result = run_backtest(model, synthetic_status)

    # Each fold learns from data strictly before Paris midnight.
    assert [cutoff for cutoff, _ in model.seen] == [
        day_bounds(date(2026, 11, 9))[0],
        day_bounds(date(2026, 11, 10))[0],
    ]
    assert all(last is not None and last < cutoff for cutoff, last in model.seen)
    assert [fold.day for fold in result.folds] == [date(2026, 11, 9), date(2026, 11, 10)]
    # 288 origins x 2 stations x 3 horizons a day. Day 10 ends at 23:00 UTC
    # and data runs to 23:55, so even its last 60-minute targets exist.
    for fold in result.folds:
        assert fold.kept == fold.candidates == 288 * 2 * 3
        assert sum(fold.excluded.values()) == 0
    assert result.frame.height == sum(fold.kept for fold in result.folds)
    assert result.frame["day"].unique().sort().to_list() == [date(2026, 11, 9), date(2026, 11, 10)]
    metrics = regression_metrics(result.frame)
    assert metrics["mechanical_mae"].to_list() == metrics["mechanical_mae_persistence"].to_list()


def test_training_examples_stop_before_the_cutoff(synthetic_status: pl.DataFrame) -> None:
    cutoff = day_bounds(date(2026, 11, 9))[0]
    data = TrainingData(
        synthetic_status.filter(pl.col("fetched_at") < cutoff),
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

    examples = data.examples(horizons=(15,))

    assert examples["target_at"].max() < cutoff  # type: ignore[operator]
    assert "suspected_frozen" not in examples.columns
    assert examples.height > 0


def test_a_model_reading_the_future_is_caught(synthetic_status: pl.DataFrame) -> None:
    with pytest.raises(LookaheadError, match="clairvoyant"):
        run_backtest(Clairvoyant(), synthetic_status)


def test_lookahead_check_passes_for_an_honest_model(synthetic_status: pl.DataFrame) -> None:
    start, _ = day_bounds(date(2026, 11, 9))
    examples = pl.DataFrame(
        {
            "station_id": [1, 1],
            "origin": [start, start + timedelta(hours=13)],
            "horizon_min": pl.Series([15, 15], dtype=pl.Int16),
            "target_at": [start + timedelta(minutes=15), start + timedelta(hours=13, minutes=15)],
            "mechanical": [1, 2],
            "ebike": [0, 0],
            "docks": [3, 0],
        }
    )

    check_no_lookahead(Persistence(), synthetic_status, examples, start + timedelta(hours=12))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p.drop("p_full"), "missing columns"),
        (lambda p: p.head(1), "predictions for"),
        (lambda p: pl.concat([p, p]), "duplicates"),
        (lambda p: p.with_columns(pred_docks=None), "null"),
        (lambda p: p.with_columns(p_empty=1.5), "outside"),
        (lambda p: p.with_columns(lo_mechanical=0.0), "incomplete intervals"),
        (
            lambda p: p.with_columns(
                **{
                    f"{b}_{n}": pl.lit(v)
                    for n in ("mechanical", "ebike", "docks")
                    for b, v in (("lo", 3.0), ("hi", 1.0))
                }
            ),
            "lower > upper",
        ),
    ],
)
def test_invalid_predictions_are_refused(edit: object, message: str) -> None:
    examples = pl.DataFrame(
        {
            "station_id": [1, 2],
            "origin": [datetime(2026, 11, 9, tzinfo=UTC)] * 2,
            "horizon_min": pl.Series([15, 15], dtype=pl.Int16),
            "mechanical": [1, 0],
            "ebike": [0, 0],
            "docks": [3, 4],
        }
    )
    predictions = Persistence().predict(pl.DataFrame(), examples)

    with pytest.raises(InvalidPredictionError, match=message):
        validate_predictions(examples, edit(predictions))  # type: ignore[operator]
