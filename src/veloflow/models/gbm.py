"""Global gradient boosting model (LightGBM), one set of models per horizon.

For each horizon, on every station at once:

* three regressors forecast the **change** of each count (mechanical bikes,
  e-bikes, docks) with an absolute-error objective, the median change, since
  the bench scores the mean absolute error; the forecast is the state at the
  origin plus that change, between 0 and a cap (as ``ProfileDelta``);
* two classifiers forecast ``empty`` and ``full``. Their probabilities are
  calibrated by isotonic regression on the most recent 20 % of the training
  origins, held out from the classifiers;
* 80 % intervals of the counts by split conformal prediction: the regressors
  learn on the older 80 % of the origins; the newest 20 % give the errors
  ``target - rounded forecast``. Counts are whole numbers, so a symmetric
  ``± k`` jumps past 80 % (``± 0`` holds 60 %, ``± 1`` 90 %). Two refinements
  (``interval_method``):

  - ``asymmetric``: the narrowest ``[forecast - a, forecast + b]`` that holds
    80 % of the errors (finite-sample corrected), ``a`` and ``b`` chosen apart;
  - ``mondrian`` (default): the same, per group of examples (Mondrian
    conformal prediction). The group is how much the count moved in the hour
    before the origin (0, 1 to 2, 3 or more): a quiet station needs a narrow
    interval, a busy one a wide one. A group with fewer than
    ``MIN_GROUP_EXAMPLES`` errors uses the bounds of all groups together.

  ``symmetric`` keeps ``± k``, for comparison.

Features: ``features.build`` plus the station profile (mean state and event
frequencies on the same type of day and slot, at the origin and at the
target), learned from training states only. For a training example the
profile leaves out the example's own day: otherwise it contains the target
itself, and with few days the model learns to copy it (measured: mean absolute
error 0.16 in sample, 1.24 on the next day, against 0.30 for persistence).
"""

import logging
from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression

from veloflow.evaluation.examples import HORIZONS_MIN, TARGETS
from veloflow.features.build import FEATURE_COLUMNS, FeatureContext, build_features
from veloflow.models.base import INPUT_COLUMNS, KEYS, TrainingData
from veloflow.models.baselines import PARIS, PROFILE_KEYS, calendar_keys

logger = logging.getLogger(__name__)

EVENTS: tuple[str, ...] = ("empty", "full")
PROFILE_COLUMNS: tuple[str, ...] = (
    *[f"profile_{n}" for n in TARGETS],
    "profile_empty",
    "profile_full",
)
MODEL_COLUMNS: tuple[str, ...] = (
    *FEATURE_COLUMNS[1:],
    *[f"target_{name}" for name in PROFILE_COLUMNS],
    *[f"origin_{name}" for name in PROFILE_COLUMNS],
)
CALIBRATION_SHARE = 0.2
INTERVAL_LEVEL = 0.8
INTERVAL_METHODS: tuple[str, ...] = ("symmetric", "asymmetric", "mondrian")
# Mondrian groups: change of the count over the hour before the origin.
ACTIVITY_LAG_MIN = 60
ACTIVITY_EDGES: tuple[int, ...] = (1, 3)  # |change| 0 | 1-2 | 3+
POOLED = -1  # the group of every example, used when a group is too small
MIN_GROUP_EXAMPLES = 500


def daily_profile_sums(states: pl.DataFrame) -> pl.DataFrame:
    """Sums and counts per station, type of day, slot and Paris date."""
    bikes = pl.col("mechanical") + pl.col("ebike")
    return (
        states.with_columns(
            *calendar_keys(pl.col("time")),
            day=pl.col("time").dt.convert_time_zone(PARIS).dt.date(),
            empty=(bikes == 0).cast(pl.Float64),
            full=(pl.col("docks") == 0).cast(pl.Float64),
        )
        .group_by(*PROFILE_KEYS, "day")
        .agg(
            pl.len().alias("count"),
            *[
                pl.col(n).cast(pl.Float64).sum().alias(f"sum_{n}")
                for n in (*TARGETS, "empty", "full")
            ],
        )
    )


def _profile_names() -> list[tuple[str, str]]:
    return [(f"sum_{n}", f"profile_{n}") for n in (*TARGETS, "empty", "full")]


def profile_features(
    frame: pl.DataFrame, sums: pl.DataFrame, time: str, prefix: str, *, leave_own_day_out: bool
) -> pl.DataFrame:
    """Join the station profile at ``time``, optionally without that day."""
    totals = sums.group_by(PROFILE_KEYS).agg(
        pl.col("count").sum(), *[pl.col(s).sum() for s, _ in _profile_names()]
    )
    frame = frame.with_columns(
        *calendar_keys(pl.col(time)), _day=pl.col(time).dt.convert_time_zone(PARIS).dt.date()
    ).join(totals, on=list(PROFILE_KEYS), how="left", maintain_order="left")
    if leave_own_day_out:
        own = sums.rename({"day": "_day", "count": "own_count"}).rename(
            {s: f"own_{s}" for s, _ in _profile_names()}
        )
        frame = frame.join(own, on=[*PROFILE_KEYS, "_day"], how="left", maintain_order="left")
        count = pl.col("count") - pl.col("own_count").fill_null(0)
        values = [
            ((pl.col(s) - pl.col(f"own_{s}").fill_null(0.0)) / count).alias(f"{prefix}_{p}")
            for s, p in _profile_names()
        ]
        drop = ["own_count", *[f"own_{s}" for s, _ in _profile_names()]]
    else:
        count = pl.col("count")
        values = [(pl.col(s) / count).alias(f"{prefix}_{p}") for s, p in _profile_names()]
        drop = []
    # No other day with this slot: unknown, not zero.
    values = [pl.when(count > 0).then(v).alias(v.meta.output_name()) for v in values]
    return frame.with_columns(values).drop(
        "day_type", "slot", "_day", "count", *[s for s, _ in _profile_names()], *drop
    )


def default_params() -> dict[str, object]:
    """LightGBM settings shared by every model (small, fast, regularised)."""
    return {
        "learning_rate": 0.05,
        "n_estimators": 300,
        "num_leaves": 63,
        "min_child_samples": 200,
        "subsample": 0.8,
        "subsample_freq": 1,
        "colsample_bytree": 0.8,
        "random_state": 0,
        "verbose": -1,
    }


@dataclass
class _HorizonModels:
    regressors: dict[str, lgb.LGBMRegressor] = field(default_factory=dict)
    classifiers: dict[str, lgb.LGBMClassifier | float] = field(default_factory=dict)
    calibrators: dict[str, IsotonicRegression | None] = field(default_factory=dict)
    half_widths: dict[str, float] = field(default_factory=dict)
    # Interval offsets per count and group: (below, above) the rounded forecast.
    bounds: dict[str, dict[int, tuple[float, float]]] = field(default_factory=dict)


def activity_group(change: pl.Expr) -> pl.Expr:
    """Mondrian group of an example: 0, 1 or 2 by the absolute change of the last hour.

    An unknown change (station seen for less than an hour) is group 0.
    """
    moved = change.abs().fill_null(0)
    group = pl.lit(0, dtype=pl.Int8)
    for index, edge in enumerate(ACTIVITY_EDGES, start=1):
        group = pl.when(moved >= edge).then(pl.lit(index, dtype=pl.Int8)).otherwise(group)
    return group


def conformal_bounds(errors: np.ndarray, level: float = INTERVAL_LEVEL) -> tuple[float, float]:
    """Narrowest ``(a, b)`` such that ``-a <= error <= b`` on a share ``level`` of new cases.

    Errors are whole numbers. The interval must hold at least the
    ``ceil((n + 1) * level)``-th error (the split conformal rank); among the
    narrowest ones, the one holding the fewest errors, the closest to
    ``level``. Infinite when there are too few errors.
    """
    n = len(errors)
    rank = int(np.ceil((n + 1) * level))
    if rank > n:
        return float("inf"), float("inf")
    values = np.round(errors).astype(np.int64)
    low, high = int(values.min()), int(values.max())
    counts = np.cumsum(np.bincount(values - low, minlength=high - low + 1))

    def held(a: int, b: int) -> int:  # errors in [-a, b]
        top = min(b, high) - low
        bottom = max(-a, low) - low - 1
        if top < 0 or top <= bottom:
            return 0
        return int(counts[top] - (counts[bottom] if bottom >= 0 else 0))

    best: tuple[int, int, int, int] | None = None  # (width, held, a, b)
    for a in range(max(0, -low) + 1):
        b = 0
        while held(a, b) < rank and b < max(0, high):
            b += 1
        if held(a, b) < rank:
            continue
        candidate = (a + b, held(a, b), a, b)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    assert best is not None  # a = -low, b = high hold every error
    return float(best[2]), float(best[3])


def interval_bounds(
    errors: np.ndarray, groups: np.ndarray, method: str, level: float = INTERVAL_LEVEL
) -> dict[int, tuple[float, float]]:
    """Offsets below and above the forecast, for every group (``POOLED``: all of them)."""
    if method == "symmetric":
        k = conformal_half_width(errors, level)
        return {POOLED: (k, k)}
    bounds = {POOLED: conformal_bounds(errors, level)}
    if method == "mondrian":
        for group in np.unique(groups):
            mask = groups == group
            if mask.sum() >= MIN_GROUP_EXAMPLES:
                bounds[int(group)] = conformal_bounds(errors[mask], level)
    return bounds


def conformal_half_width(errors: np.ndarray, level: float = INTERVAL_LEVEL) -> float:
    """Smallest ``k`` such that ``|error| <= k`` on a share ``level`` of new cases.

    Split conformal prediction: the ``ceil((n + 1) * level)``-th smallest
    absolute error on held-out examples, or infinity when there are too few.
    """
    n = len(errors)
    rank = int(np.ceil((n + 1) * level))
    if rank > n:
        return float("inf")
    return float(np.sort(np.abs(errors))[rank - 1])


class GradientBoosting:
    """LightGBM on point-in-time features, calibrated event probabilities."""

    name = "lightgbm"

    def __init__(
        self,
        context: FeatureContext,
        *,
        max_rows_per_horizon: int = 400_000,
        params: dict[str, object] | None = None,
        seed: int = 0,
        interval_method: str = "mondrian",
    ) -> None:
        """Set the static context, the training budget and how intervals are built."""
        if interval_method not in INTERVAL_METHODS:
            raise ValueError(f"interval_method must be one of {INTERVAL_METHODS}")
        self.interval_method = interval_method
        self.context = context
        self.max_rows = max_rows_per_horizon
        self.params = default_params() if params is None else params
        self.seed = seed
        self.sums: pl.DataFrame | None = None
        self.capacity: pl.DataFrame | None = None
        self.models: dict[int, _HorizonModels] = {}
        self.trained_examples: dict[int, int] = {}

    def _matrix(
        self, examples: pl.DataFrame, history: pl.DataFrame, *, training: bool
    ) -> pl.DataFrame:
        """Features plus the station profile at the origin and target slots."""
        assert self.sums is not None  # set by fit; predict checks it first
        features = build_features(examples, history, self.context)
        frame = features.with_columns(examples.select("target_at"))
        for end, time in (("target", "target_at"), ("origin", "origin")):
            frame = profile_features(frame, self.sums, time, end, leave_own_day_out=training)
        return frame

    @staticmethod
    def _x(frame: pl.DataFrame) -> np.ndarray:
        return frame.select([pl.col(c).cast(pl.Float32) for c in MODEL_COLUMNS]).to_numpy()

    def fit(self, data: TrainingData) -> None:
        """Train the regressors and calibrated classifiers of every horizon."""
        states = data.states()
        self.sums = daily_profile_sums(states)
        self.capacity = states.group_by("station_id").agg(
            (pl.col("mechanical") + pl.col("ebike") + pl.col("docks"))
            .max()
            .cast(pl.Float64)
            .alias("capacity")
        )
        # Built and sampled one day at a time: memory follows the budget.
        examples = data.sampled_examples(self.max_rows, seed=self.seed)
        self.models = {}
        self.trained_examples = {}
        for horizon in HORIZONS_MIN:
            subset = examples.filter(pl.col("horizon_min") == horizon)
            if subset.height > self.max_rows:
                subset = subset.sample(self.max_rows, seed=self.seed)
            subset = subset.sort("origin")
            if subset.height == 0:
                continue
            frame = self._matrix(subset.select(INPUT_COLUMNS), data.history, training=True)
            x = self._x(frame)
            models = _HorizonModels()
            # Models learn on the older origins; calibration on the newest.
            split = int(subset.height * (1 - CALIBRATION_SHARE))
            held_out = subset[split:].join(
                self.capacity, on="station_id", how="left", maintain_order="left"
            )
            cap = (
                held_out.select(
                    pl.max_horizontal(
                        "capacity", (pl.col("mechanical") + pl.col("ebike") + pl.col("docks"))
                    )
                )
                .to_series()
                .cast(pl.Float64)
                .to_numpy()
            )
            for name in TARGETS:
                change = (subset[f"y_{name}"] - subset[name]).cast(pl.Float32).to_numpy()
                regressor = lgb.LGBMRegressor(objective="l1", **self.params)  # type: ignore[arg-type]
                models.regressors[name] = regressor.fit(x[:split], change[:split])
                state = held_out[name].cast(pl.Float64).to_numpy()
                forecast = np.minimum(
                    np.maximum(state + np.asarray(regressor.predict(x[split:])), 0.0), cap
                )
                target = held_out[f"y_{name}"].cast(pl.Float64).to_numpy()
                errors = target - np.round(forecast)
                groups = (
                    frame[split:]
                    .select(activity_group(pl.col(f"d_{name}_{ACTIVITY_LAG_MIN}")))
                    .to_series()
                    .to_numpy()
                )
                models.half_widths[name] = conformal_half_width(errors)
                models.bounds[name] = interval_bounds(errors, groups, self.interval_method)
            for event in EVENTS:
                y = subset[f"y_{event}"].to_numpy().astype(np.int8)
                if y[:split].min() == y[:split].max():
                    models.classifiers[event] = float(y.mean())
                    models.calibrators[event] = None
                    continue
                classifier = lgb.LGBMClassifier(objective="binary", **self.params)  # type: ignore[arg-type]
                classifier.fit(x[:split], y[:split])
                models.classifiers[event] = classifier
                raw = np.asarray(classifier.predict_proba(x[split:]))[:, 1]
                if split < len(y) and y[split:].min() != y[split:].max():
                    calibrator = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
                    models.calibrators[event] = calibrator.fit(raw, y[split:])
                else:
                    models.calibrators[event] = None
            self.models[horizon] = models
            self.trained_examples[horizon] = subset.height
            logger.info("%s: horizon %d trained on %d examples", self.name, horizon, subset.height)

    def compact(self) -> None:
        """Keep only what forecasting needs: profile totals, not daily sums.

        Daily sums serve the leave-one-day-out profile of training; forecasts
        use totals. One row per station, type of day and slot instead of one
        per day keeps a saved model small whatever the length of the history.
        """
        if self.sums is None:
            raise RuntimeError("fit must be called before compact")
        names = ["count", *[s for s, _ in _profile_names()]]
        self.sums = (
            self.sums.group_by(PROFILE_KEYS)
            .agg([pl.col(name).sum() for name in names])
            .with_columns(day=pl.lit(None, dtype=pl.Date))
            .select(self.sums.columns)
        )

    def _bounds_of(self, models: _HorizonModels, name: str) -> dict[int, tuple[float, float]]:
        """Interval offsets of a count; ``± k`` for a model saved before ``bounds`` existed."""
        bounds = getattr(models, "bounds", {}).get(name)
        if bounds:
            return dict(bounds)
        k = models.half_widths.get(name, float("inf"))
        return {POOLED: (k, k)}

    def _with_bounds(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Add ``a_<count>`` and ``b_<count>``: offsets below and above, per row's group."""
        groups = range(len(ACTIVITY_EDGES) + 1)
        for name in TARGETS:
            rows = []
            for horizon, models in self.models.items():
                bounds = self._bounds_of(models, name)
                for group in groups:
                    below, above = bounds.get(group, bounds[POOLED])
                    rows.append((horizon, group, below, above))
            table = pl.DataFrame(
                rows,
                schema={
                    "horizon_min": pl.Int16,
                    f"g_{name}": pl.Int8,
                    f"a_{name}": pl.Float64,
                    f"b_{name}": pl.Float64,
                },
                orient="row",
            )
            frame = frame.join(
                table, on=["horizon_min", f"g_{name}"], how="left", maintain_order="left"
            ).with_columns(
                pl.col(f"a_{name}").fill_null(float("inf")),
                pl.col(f"b_{name}").fill_null(float("inf")),
            )
        return frame

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """Forecast counts and event probabilities of ``examples``."""
        if self.sums is None or self.capacity is None:
            raise RuntimeError("fit must be called before predict")
        frame = self._matrix(examples, history, training=False).join(
            self.capacity, on="station_id", how="left", maintain_order="left"
        )
        held = (pl.col("mechanical") + pl.col("ebike") + pl.col("docks")).cast(pl.Float64)
        frame = frame.with_columns(
            cap=pl.max_horizontal("capacity", held), _row=pl.int_range(pl.len())
        )
        parts = []
        for horizon in frame["horizon_min"].unique().to_list():
            part = frame.filter(pl.col("horizon_min") == horizon)
            models = self.models.get(int(horizon))
            if models is None:
                raise RuntimeError(f"no model for horizon {horizon}")
            x = self._x(part)
            columns: dict[str, np.ndarray] = {}
            for name in TARGETS:
                columns[f"change_{name}"] = np.asarray(models.regressors[name].predict(x))
            for event in EVENTS:
                model = models.classifiers[event]
                if isinstance(model, float):
                    p = np.full(part.height, model)
                else:
                    p = np.asarray(model.predict_proba(x))[:, 1]
                    calibrator = models.calibrators[event]
                    if calibrator is not None:
                        p = np.asarray(calibrator.predict(p))
                columns[f"p_{event}"] = p
            parts.append(
                part.select(
                    "_row",
                    *KEYS,
                    *TARGETS,
                    "cap",
                    *[f"d_{n}_{ACTIVITY_LAG_MIN}" for n in TARGETS],
                ).with_columns([pl.Series(k, v, dtype=pl.Float64) for k, v in columns.items()])
            )
        if not parts:
            return examples.select(KEYS).with_columns(
                [
                    pl.lit(None, dtype=pl.Float64).alias(c)
                    for c in ("pred_mechanical", "pred_ebike", "pred_docks", "p_empty", "p_full")
                ]
            )
        joined = pl.concat(parts).sort("_row")
        forecasts = joined.with_columns(
            [
                pl.min_horizontal(
                    (pl.col(name).cast(pl.Float64) + pl.col(f"change_{name}")).clip(
                        lower_bound=0.0
                    ),
                    pl.col("cap"),
                ).alias(f"pred_{name}")
                for name in TARGETS
            ]
        )
        forecasts = forecasts.with_columns(
            [activity_group(pl.col(f"d_{n}_{ACTIVITY_LAG_MIN}")).alias(f"g_{n}") for n in TARGETS]
        )
        forecasts = self._with_bounds(forecasts)
        return forecasts.select(
            *KEYS,
            *[f"pred_{name}" for name in TARGETS],
            pl.col("p_empty").clip(0.0, 1.0),
            pl.col("p_full").clip(0.0, 1.0),
            *[
                expr
                for name in TARGETS
                for expr in (
                    (pl.col(f"pred_{name}").round() - pl.col(f"a_{name}"))
                    .clip(lower_bound=0.0)
                    .alias(f"lo_{name}"),
                    pl.min_horizontal(
                        pl.col(f"pred_{name}").round() + pl.col(f"b_{name}"), pl.col("cap")
                    ).alias(f"hi_{name}"),
                )
            ],
        )
