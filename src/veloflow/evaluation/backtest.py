"""Rolling-origin backtest, one Paris day at a time.

For each test day ``D``, the forecaster learns from the data fetched before
``D`` (midnight, Paris time), then forecasts every example whose origin falls
in ``D``. The training window grows day after day; examples are built one test
day at a time so that memory does not grow with the history.

The quality masks (rules R4a and R4b) are computed once on all the data. They
only decide which rows are trusted, and never feed a value to a forecaster.

A forecaster must not read the future. ``check_no_lookahead`` makes it
predict twice, once with the whole test day and once with history and
examples cut at a time ``C``: forecasts made at or before ``C`` must not
change.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from veloflow.datasets import resample_status
from veloflow.evaluation.examples import EXCLUSION_REASONS, HORIZONS_MIN, build_examples
from veloflow.models.base import (
    INPUT_COLUMNS,
    KEYS,
    PREDICTION_COLUMNS,
    Forecaster,
    TrainingData,
)
from veloflow.quality.freeze import change_counts, frozen_feed_slots, suspected_frozen_stations

logger = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")
GRID_TOLERANCE = timedelta(minutes=5)
# A Paris day is a test day when at least this share of its 5-minute slots
# holds a version (rule R6 reports the gaps in detail).
MIN_DAY_COVERAGE = 0.9
COVERAGE_SLOT = timedelta(minutes=5)


class InvalidPredictionError(ValueError):
    """A forecaster returned predictions the bench cannot score."""


class LookaheadError(AssertionError):
    """A forecaster used data from after the time of its forecast."""


@dataclass(frozen=True, slots=True)
class FoldSummary:
    """What one test day contributed."""

    day: date
    train_versions: int
    candidates: int
    kept: int
    excluded: dict[str, int]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Scored examples of every test day and a summary per day."""

    model: str
    frame: pl.DataFrame
    folds: list[FoldSummary]


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Start and end of a Paris day, in UTC (23 or 25 hours on clock changes)."""
    start = datetime.combine(day, time(), tzinfo=PARIS)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=PARIS)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))


def evaluation_days(status: pl.DataFrame, min_coverage: float = MIN_DAY_COVERAGE) -> list[date]:
    """Paris days of ``status`` that are covered and have data before them.

    A day is covered when at least ``min_coverage`` of its 5-minute slots
    hold a version: a partial first day, or a day the collector missed, is
    not a test day.
    """
    if status.height == 0:
        return []
    first = status["fetched_at"].min()
    slots = (
        status.select(pl.col("fetched_at").dt.truncate("5m").unique())
        .with_columns(day=pl.col("fetched_at").dt.convert_time_zone(str(PARIS)).dt.date())
        .group_by("day")
        .len()
    )
    days = []
    for day, count in sorted(slots.iter_rows()):
        start, end = day_bounds(day)
        expected = (end - start) / COVERAGE_SLOT
        if count >= min_coverage * expected and first < start:  # type: ignore[operator]
            days.append(day)
    return days


def validate_predictions(examples: pl.DataFrame, predictions: pl.DataFrame) -> pl.DataFrame:
    """Join predictions to examples, refusing anything incomplete or out of range.

    Raises:
        InvalidPredictionError: missing columns, missing or duplicated
            examples, null values, or a probability outside [0, 1].
    """
    missing = [name for name in (*KEYS, *PREDICTION_COLUMNS) if name not in predictions.columns]
    if missing:
        raise InvalidPredictionError(f"missing columns: {missing}")
    predictions = predictions.select(*KEYS, *PREDICTION_COLUMNS)
    if predictions.height != examples.height or predictions.select(KEYS).is_duplicated().any():
        raise InvalidPredictionError(
            f"{predictions.height} predictions for {examples.height} examples, or duplicates"
        )
    joined = examples.join(predictions, on=list(KEYS), how="left")
    if joined.select(PREDICTION_COLUMNS).null_count().sum_horizontal().item() > 0:
        raise InvalidPredictionError("some examples have no or null predictions")
    outside = joined.filter(
        ~pl.col("p_empty").is_between(0.0, 1.0) | ~pl.col("p_full").is_between(0.0, 1.0)
    ).height
    if outside:
        raise InvalidPredictionError(f"{outside} probabilities outside [0, 1]")
    return joined


def check_no_lookahead(
    model: Forecaster, history: pl.DataFrame, examples: pl.DataFrame, cut: datetime
) -> None:
    """Fail if forecasts made at or before ``cut`` depend on later data.

    Args:
        model: a fitted forecaster.
        history: history given to ``predict`` for the whole test day.
        examples: ``INPUT_COLUMNS`` of the test day.
        cut: a time inside the test day.

    Raises:
        LookaheadError: a forecast made at or before ``cut`` changed when the
            data after ``cut`` was removed.
    """
    full = model.predict(history, examples)
    before = examples.filter(pl.col("origin") <= cut)
    truncated = model.predict(history.filter(pl.col("fetched_at") <= cut), before)
    compared = before.select(KEYS).join(full, on=list(KEYS)).sort(KEYS)
    reference = before.select(KEYS).join(truncated, on=list(KEYS)).sort(KEYS)
    if not compared.select(PREDICTION_COLUMNS).equals(reference.select(PREDICTION_COLUMNS)):
        raise LookaheadError(f"{model.name}: forecasts made before {cut} use later data")


def run_backtest(
    model: Forecaster,
    status: pl.DataFrame,
    *,
    days: list[date] | None = None,
    horizons: tuple[int, ...] = HORIZONS_MIN,
    lookahead_check: bool = True,
) -> BacktestResult:
    """Evaluate ``model`` on every test day of ``status``.

    Args:
        model: the forecaster to evaluate.
        status: rows from ``datasets.load_station_status``.
        days: test days (Paris); by default ``evaluation_days(status)``.
        horizons: forecast horizons in minutes.
        lookahead_check: run ``check_no_lookahead`` on the first test day,
            cut at noon.

    Returns:
        Kept examples of every test day with targets, ``suspected_frozen``,
        ``day`` and predictions; one summary per test day.
    """
    frozen_slots = frozen_feed_slots(change_counts(status))
    frozen_stations = suspected_frozen_stations(status)
    selected = evaluation_days(status) if days is None else days
    max_horizon = timedelta(minutes=max(horizons))
    scored: list[pl.DataFrame] = []
    folds: list[FoldSummary] = []
    for index, day in enumerate(selected):
        start, end = day_bounds(day)
        history = status.filter(pl.col("fetched_at") < start)
        model.fit(TrainingData(history, frozen_slots, frozen_stations, start))

        window = status.filter(
            pl.col("fetched_at").is_between(start - GRID_TOLERANCE, end + max_horizon)
        )
        built = build_examples(
            resample_status(window),
            horizons=horizons,
            frozen_slots=frozen_slots,
            frozen_stations=frozen_stations,
            origin_range=(start, end),
        )
        examples = built.frame
        inputs = examples.select(INPUT_COLUMNS)
        visible = status.filter(pl.col("fetched_at") < end)
        if lookahead_check and index == 0:
            check_no_lookahead(model, visible, inputs, start + timedelta(hours=12))
        predictions = model.predict(visible, inputs)
        scored.append(
            validate_predictions(examples, predictions).with_columns(day=pl.lit(day, dtype=pl.Date))
        )
        folds.append(
            FoldSummary(
                day=day,
                train_versions=history.select("fetched_at").n_unique(),
                candidates=built.candidates,
                kept=examples.height,
                excluded={name: built.excluded[name] for name in EXCLUSION_REASONS},
            )
        )
        logger.info("%s on %s: %d examples", model.name, day, examples.height)
    frame = pl.concat(scored) if scored else pl.DataFrame()
    return BacktestResult(model=model.name, frame=frame, folds=folds)
