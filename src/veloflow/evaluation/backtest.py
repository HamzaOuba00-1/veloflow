"""Rolling-origin backtest, one Paris day at a time.

For each test day ``D``, the forecaster learns from the data fetched before
``D`` (midnight, Paris time), at most ``TRAIN_DAYS`` days of it, then forecasts
every example whose origin falls in ``D``.

The data is read a window at a time (``SilverSource``): one test day and its
training window are in memory, never the whole history, so a run over six
weeks needs no more memory than over two. ``FrameSource`` serves rows already
in memory (tests, small runs) the same way.

The quality masks (rules R4a and R4b) are computed once on all the data, one
day at a time with a margin (``quality_masks``), and give the same masks as a
computation on everything at once. They only decide which rows are trusted,
and never feed a value to a forecaster.

A forecaster must not read the future. ``check_no_lookahead`` makes it
predict twice, once with the whole test day and once with history and
examples cut at a time ``C``: forecasts made at or before ``C`` must not
change.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import polars as pl

from veloflow.datasets import load_station_status, resample_status, silver_days
from veloflow.evaluation.examples import EXCLUSION_REASONS, HORIZONS_MIN, build_examples
from veloflow.models.base import (
    INPUT_COLUMNS,
    INTERVAL_COLUMNS,
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
# Days of history a forecaster learns from at each test day: bounds memory and
# time as the history grows (a profile needs a few weeks, not every day).
TRAIN_DAYS = 28
# Rules R4a and R4b computed day by day see this much data before and after
# the day, so that their result equals a computation on everything at once
# (R4b needs 12 daytime hours of a run; two days hold at least 30).
MASK_BEFORE = timedelta(days=2)
MASK_AFTER = timedelta(hours=2)
DAY = timedelta(days=1)
PERIOD_SCHEMA: dict[str, pl.DataType] = {
    "station_id": pl.Int64(),
    "frozen_from": pl.Datetime("us", "UTC"),
    "frozen_until": pl.Datetime("us", "UTC"),
    "daytime_hours": pl.Float64(),
}


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


class StatusSource(Protocol):
    """Cleaned ``station_status`` rows, read one window at a time."""

    def days(self) -> list[date]:
        """UTC days that hold data, in order."""
        ...

    def load(self, start: datetime, end: datetime) -> pl.DataFrame:
        """Rows fetched in ``[start, end)``."""
        ...


@dataclass(frozen=True, slots=True)
class FrameSource:
    """Rows already in memory (rows from ``datasets.load_station_status``)."""

    frame: pl.DataFrame

    def days(self) -> list[date]:
        """UTC days of the rows."""
        if self.frame.height == 0:
            return []
        dates = self.frame.select(pl.col("fetched_at").dt.date().unique().sort())
        return list(dates.to_series())

    def load(self, start: datetime, end: datetime) -> pl.DataFrame:
        """Rows fetched in ``[start, end)``."""
        return self.frame.filter(pl.col("fetched_at").is_between(start, end, closed="left"))


@dataclass(frozen=True, slots=True)
class SilverSource:
    """Silver ``station_status`` of ``data_dir``, from ``first`` to ``last`` (UTC days).

    Each window is read with the day before it, so that rule R1 compares its
    first versions with the previous ones, as on a single read.
    """

    data_dir: Path
    first: date | None = None
    last: date | None = None

    def days(self) -> list[date]:
        """Silver days within ``[first, last]``."""
        return [
            day
            for day in silver_days(self.data_dir)
            if (self.first is None or day >= self.first) and (self.last is None or day <= self.last)
        ]

    def load(self, start: datetime, end: datetime) -> pl.DataFrame:
        """Rows fetched in ``[start, end)``, within ``[first, last]``."""
        low = (start - DAY).date()
        high = (end - timedelta(microseconds=1)).date()
        if self.first is not None:
            low = max(low, self.first)
        if self.last is not None:
            high = min(high, self.last)
        frame = load_station_status(self.data_dir, low, high).frame
        return frame.filter(pl.col("fetched_at").is_between(start, end, closed="left"))


@dataclass(frozen=True, slots=True)
class QualityMasks:
    """Rules R4a and R4b over every day of a source, and the fetch times."""

    frozen_slots: pl.DataFrame
    frozen_stations: pl.DataFrame
    fetch_times: pl.DataFrame


def _merge_periods(periods: pl.DataFrame) -> pl.DataFrame:
    """Join the day pieces of a period back together (a piece ends where the next starts)."""
    if periods.height == 0:
        return periods
    return (
        periods.sort("station_id", "frozen_from")
        .with_columns(
            piece=(pl.col("frozen_from") != pl.col("frozen_until").shift(1).over("station_id"))
            .fill_null(value=True)
            .cum_sum()
            .over("station_id")
        )
        .group_by("station_id", "piece", maintain_order=True)
        .agg(
            pl.col("frozen_from").min(),
            pl.col("frozen_until").max(),
            # Longest piece seen: a window shows at most a few days of a run.
            pl.col("daytime_hours").max(),
        )
        .drop("piece")
    )


def quality_masks(source: StatusSource) -> QualityMasks:
    """Rules R4a and R4b over every day of ``source``, one day in memory at a time.

    Each UTC day is computed with ``MASK_BEFORE`` and ``MASK_AFTER`` of data
    around it, then cut to the day: the counts of changes per version (R4a)
    and the suspected frozen periods (R4b) equal those of one computation on
    everything.
    """
    counts, periods, times = [], [], []
    for day in source.days():
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + DAY
        frame = source.load(start - MASK_BEFORE, end + MASK_AFTER)
        in_day = pl.col("fetched_at").is_between(start, end, closed="left")
        counts.append(change_counts(frame).filter(in_day))
        times.append(frame.filter(in_day).select(pl.col("fetched_at").unique()))
        periods.append(
            suspected_frozen_stations(frame)
            .with_columns(
                frozen_from=pl.max_horizontal("frozen_from", pl.lit(start)),
                frozen_until=pl.min_horizontal("frozen_until", pl.lit(end)),
            )
            .filter(pl.col("frozen_from") < pl.col("frozen_until"))
        )
    count_frame = (
        pl.concat(counts)
        if counts
        else pl.DataFrame(schema={"fetched_at": pl.Datetime("us", "UTC"), "changed": pl.Int64})
    )
    return QualityMasks(
        frozen_slots=frozen_feed_slots(count_frame),
        frozen_stations=(
            _merge_periods(pl.concat(periods)) if periods else pl.DataFrame(schema=PERIOD_SCHEMA)
        ),
        fetch_times=(
            pl.concat(times)
            if times
            else pl.DataFrame(schema={"fetched_at": pl.Datetime("us", "UTC")})
        ),
    )


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

    Interval columns (``INTERVAL_COLUMNS``) are kept when a model returns all
    of them.

    Raises:
        InvalidPredictionError: missing columns, missing or duplicated
            examples, null values, a probability outside [0, 1], or an
            interval whose lower bound is above its upper bound.
    """
    missing = [name for name in (*KEYS, *PREDICTION_COLUMNS) if name not in predictions.columns]
    if missing:
        raise InvalidPredictionError(f"missing columns: {missing}")
    intervals = [name for name in INTERVAL_COLUMNS if name in predictions.columns]
    if intervals and len(intervals) != len(INTERVAL_COLUMNS):
        raise InvalidPredictionError(f"incomplete intervals: {intervals}")
    predictions = predictions.select(*KEYS, *PREDICTION_COLUMNS, *intervals)
    if predictions.height != examples.height or predictions.select(KEYS).is_duplicated().any():
        raise InvalidPredictionError(
            f"{predictions.height} predictions for {examples.height} examples, or duplicates"
        )
    joined = examples.join(predictions, on=list(KEYS), how="left")
    if joined.select(*PREDICTION_COLUMNS, *intervals).null_count().sum_horizontal().item() > 0:
        raise InvalidPredictionError("some examples have no or null predictions")
    if intervals:
        reversed_bounds = joined.filter(
            pl.any_horizontal(
                [pl.col(f"lo_{n}") > pl.col(f"hi_{n}") for n in ("mechanical", "ebike", "docks")]
            )
        ).height
        if reversed_bounds:
            raise InvalidPredictionError(f"{reversed_bounds} intervals with lower > upper bound")
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
    status: pl.DataFrame | StatusSource,
    *,
    days: list[date] | None = None,
    horizons: tuple[int, ...] = HORIZONS_MIN,
    lookahead_check: bool = True,
    train_days: int | None = TRAIN_DAYS,
    masks: QualityMasks | None = None,
) -> BacktestResult:
    """Evaluate ``model`` on every test day of ``status``.

    Args:
        model: the forecaster to evaluate.
        status: rows from ``datasets.load_station_status``, or a source that
            reads them a window at a time (``SilverSource``).
        days: test days (Paris); by default ``evaluation_days`` of the data.
        horizons: forecast horizons in minutes.
        lookahead_check: run ``check_no_lookahead`` on the first test day,
            cut at noon.
        train_days: days of history before each test day the model learns
            from; ``None`` for all of it.
        masks: ``quality_masks(source)``, to share it between models.

    Returns:
        Kept examples of every test day with targets, ``suspected_frozen``,
        ``day`` and predictions; one summary per test day.
    """
    source = FrameSource(status) if isinstance(status, pl.DataFrame) else status
    masks = quality_masks(source) if masks is None else masks
    frozen_slots, frozen_stations = masks.frozen_slots, masks.frozen_stations
    selected = evaluation_days(masks.fetch_times) if days is None else days
    max_horizon = timedelta(minutes=max(horizons))
    lookback = DAY * train_days if train_days is not None else None
    all_days = source.days()
    data_start = (
        datetime.combine(all_days[0], time(), tzinfo=UTC) if all_days else datetime.now(UTC)
    )
    scored: list[pl.DataFrame] = []
    folds: list[FoldSummary] = []
    for index, day in enumerate(selected):
        start, end = day_bounds(day)
        # One read per test day: the training window, the day and its last targets.
        first = start - lookback if lookback is not None else data_start
        status = source.load(first, end + max_horizon + GRID_TOLERANCE)
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
