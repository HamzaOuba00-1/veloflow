"""The interface every forecaster implements, so the bench can compare them.

A forecaster learns from a ``TrainingData`` (history strictly before a
cutoff) and predicts a batch of examples stripped of their targets. For each
example it returns the three counts and the probabilities of the two events.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import polars as pl

from veloflow.datasets import resample_status
from veloflow.evaluation.examples import HORIZONS_MIN, TARGETS, build_examples, flag_grid

KEYS: tuple[str, ...] = ("station_id", "origin", "horizon_min")
# What a forecaster may see of an example: when, for which station, and the
# state known at the origin. Targets and quality flags stay with the bench.
INPUT_COLUMNS: tuple[str, ...] = (*KEYS, "target_at", "mechanical", "ebike", "docks")
PREDICTION_COLUMNS: tuple[str, ...] = (
    "pred_mechanical",
    "pred_ebike",
    "pred_docks",
    "p_empty",
    "p_full",
)
# Optional 80 % prediction intervals of the counts: a model returns all of
# them or none.
INTERVAL_COLUMNS: tuple[str, ...] = tuple(
    f"{bound}_{name}" for name in ("mechanical", "ebike", "docks") for bound in ("lo", "hi")
)


@dataclass(frozen=True, slots=True)
class TrainingData:
    """Everything a forecaster may learn from: data strictly before ``cutoff``.

    Attributes:
        history: cleaned ``station_status`` rows fetched before ``cutoff``.
        frozen_slots: rule R4a flags (``quality.freeze.frozen_feed_slots``).
        frozen_stations: rule R4b periods
            (``quality.freeze.suspected_frozen_stations``).
        cutoff: start of the test day (UTC).
    """

    history: pl.DataFrame
    frozen_slots: pl.DataFrame
    frozen_stations: pl.DataFrame
    cutoff: datetime

    def states(self) -> pl.DataFrame:
        """Trustworthy states on the 5-minute grid, before ``cutoff``.

        Known, operational (R3), outside a frozen feed (R4a) and not suspected
        frozen (R4b). Columns ``station_id``, ``time`` and the three counts.
        """
        grid = resample_status(self.history.filter(pl.col("fetched_at") < self.cutoff))
        flags = flag_grid(grid, self.frozen_slots, self.frozen_stations)
        return flags.filter(
            pl.col("known") & pl.col("operational") & ~pl.col("frozen_feed") & ~pl.col("suspected")
        ).select("station_id", "time", *TARGETS)

    def examples(self, horizons: tuple[int, ...] = HORIZONS_MIN) -> pl.DataFrame:
        """Training examples, built on demand: every target is before ``cutoff``.

        Suspected frozen stations (R4b) are left out, as in the headline
        results.
        """
        grid = resample_status(self.history.filter(pl.col("fetched_at") < self.cutoff))
        examples = build_examples(
            grid,
            horizons=horizons,
            frozen_slots=self.frozen_slots,
            frozen_stations=self.frozen_stations,
        ).frame
        return examples.filter(
            ~pl.col("suspected_frozen") & (pl.col("target_at") < self.cutoff)
        ).drop("suspected_frozen")

    def sampled_examples(
        self, max_rows: int, *, seed: int = 0, horizons: tuple[int, ...] = HORIZONS_MIN
    ) -> pl.DataFrame:
        """At most about ``max_rows`` training examples per horizon, built one day at a time.

        Every day of the history gets the same share of the budget, so the
        examples of one day are built, sampled and released before the next:
        memory follows the budget, not the length of the history. A history
        that fits the budget gives the same examples as ``examples``.
        """
        grid = resample_status(self.history.filter(pl.col("fetched_at") < self.cutoff))
        if grid.height == 0:
            return self.examples(horizons)
        days = grid.select(pl.col("time").dt.truncate("1d").unique().sort()).to_series().to_list()
        share = math.ceil(max_rows / len(days))
        reach = timedelta(minutes=max(horizons))
        parts = []
        for index, day in enumerate(days):
            end = day + timedelta(days=1)
            built = build_examples(
                grid.filter(pl.col("time").is_between(day, end + reach, closed="left")),
                horizons=horizons,
                frozen_slots=self.frozen_slots,
                frozen_stations=self.frozen_stations,
                origin_range=(day, end),
            ).frame.filter(~pl.col("suspected_frozen") & (pl.col("target_at") < self.cutoff))
            for horizon in horizons:
                subset = built.filter(pl.col("horizon_min") == horizon)
                if subset.height > share:
                    subset = subset.sample(share, seed=seed + index)
                parts.append(subset.drop("suspected_frozen"))
        return pl.concat(parts).sort("horizon_min", "station_id", "origin")


class Forecaster(Protocol):
    """A model the bench can evaluate."""

    name: str

    def fit(self, data: TrainingData) -> None:
        """Learn from data strictly before ``data.cutoff``."""

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """Forecast a batch of examples.

        Args:
            history: cleaned ``station_status`` rows up to the end of the test
                day. A forecast made at ``origin`` must only use rows fetched
                at or before ``origin``; the bench checks it.
            examples: ``INPUT_COLUMNS`` of the examples to forecast.

        Returns:
            ``KEYS`` and ``PREDICTION_COLUMNS``, one row per example.
        """
        ...
