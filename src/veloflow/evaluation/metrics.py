"""Forecast metrics, computed per group (horizon, hour, type of day...).

Counts: mean absolute error (MAE) and root mean squared error (RMSE), next to
the MAE of persistence on the same examples, so every table carries its
reference. Events (``empty``, ``full``): Brier score, log loss, expected
calibration error (ECE) and area under the precision-recall curve (average
precision), which suits rare events better than ROC AUC.
"""

from collections.abc import Sequence

import polars as pl

from veloflow.evaluation.examples import TARGETS

PARIS = "Europe/Paris"
EVENTS: tuple[str, ...] = ("empty", "full")
# Probabilities are clipped for the log loss, so that a confident miss costs
# about 13.8 instead of infinity.
EPSILON = 1e-6
ECE_BINS = 10


def with_groups(frame: pl.DataFrame) -> pl.DataFrame:
    """Add ``hour`` (Paris hour of the origin) and ``day_type`` (weekday or weekend)."""
    local = pl.col("origin").dt.convert_time_zone(PARIS)
    return frame.with_columns(
        hour=local.dt.hour(),
        day_type=pl.when(local.dt.weekday() >= 6)
        .then(pl.lit("weekend"))
        .otherwise(pl.lit("weekday")),
    )


def regression_metrics(frame: pl.DataFrame, by: Sequence[str] = ("horizon_min",)) -> pl.DataFrame:
    """MAE and RMSE of each count, and the MAE of persistence.

    Args:
        frame: examples with targets (``y_*``), the state at the origin and
            predictions (``pred_*``).
        by: grouping columns.
    """
    exprs: list[pl.Expr] = [pl.len().alias("n")]
    for name in TARGETS:
        error = pl.col(f"pred_{name}") - pl.col(f"y_{name}")
        exprs += [
            error.abs().mean().alias(f"{name}_mae"),
            error.pow(2).mean().sqrt().alias(f"{name}_rmse"),
            (pl.col(name) - pl.col(f"y_{name}")).abs().mean().alias(f"{name}_mae_persistence"),
        ]
    return frame.group_by(by).agg(exprs).sort(by)


def _event_metrics(frame: pl.DataFrame, by: list[str], event: str) -> pl.DataFrame:
    p = pl.col(f"p_{event}").clip(0.0, 1.0)
    y = pl.col(f"y_{event}").cast(pl.Float64)
    clipped = p.clip(EPSILON, 1.0 - EPSILON)
    scores = frame.group_by(by).agg(
        y.mean().alias(f"{event}_rate"),
        (p - y).pow(2).mean().alias(f"{event}_brier"),
        (-(y * clipped.log() + (1.0 - y) * (1.0 - clipped).log()))
        .mean()
        .alias(f"{event}_log_loss"),
    )
    # ECE: equal-width bins, gap between mean forecast and observed frequency,
    # weighted by the share of examples in the bin.
    ece = (
        frame.with_columns(bin=(p * ECE_BINS).floor().clip(0, ECE_BINS - 1))
        .group_by([*by, "bin"])
        .agg(pl.len().alias("count"), p.mean().alias("p"), y.mean().alias("y"))
        .with_columns(weight=pl.col("count") / pl.col("count").sum().over(by))
        .group_by(by)
        .agg((pl.col("weight") * (pl.col("p") - pl.col("y")).abs()).sum().alias(f"{event}_ece"))
    )
    # Average precision, with tied probabilities forming a single threshold
    # (same definition as scikit-learn's average_precision_score).
    thresholds = (
        frame.group_by([*by, p.alias("p")])
        .agg(y.sum().alias("tp"), pl.len().alias("count"))
        .sort([*by, "p"], descending=[False] * len(by) + [True])
        .with_columns(
            precision=pl.col("tp").cum_sum().over(by) / pl.col("count").cum_sum().over(by),
            positives=pl.col("tp").sum().over(by),
        )
    )
    average_precision = thresholds.group_by(by).agg(
        pl.when(pl.col("positives").first() > 0)
        .then((pl.col("precision") * pl.col("tp")).sum() / pl.col("positives").first())
        .alias(f"{event}_auc_pr")
    )
    return scores.join(ece, on=by).join(average_precision, on=by)


def event_metrics(frame: pl.DataFrame, by: Sequence[str] = ("horizon_min",)) -> pl.DataFrame:
    """Brier score, log loss, ECE and average precision of both events.

    Args:
        frame: examples with ``y_empty``, ``y_full``, ``p_empty``, ``p_full``.
        by: grouping columns.

    Returns:
        One row per group: ``n``, then for each event its observed ``rate``,
        ``brier``, ``log_loss``, ``ece`` and ``auc_pr`` (null without any
        positive example).
    """
    keys = list(by)
    result = frame.group_by(keys).agg(pl.len().alias("n"))
    for event in EVENTS:
        result = result.join(_event_metrics(frame, keys, event), on=keys)
    return result.sort(keys)
