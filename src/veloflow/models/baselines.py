"""Baselines: the references every model must beat (design, section 4.5).

* ``Persistence``: nothing changes. Probabilities are 0 or 1.
* ``StationProfile``: the average of the station on the same type of day and
  15-minute slot as the target, from training days only. Its event
  probabilities are the historical frequencies of the slot, the reference of
  acceptance criterion 3.
* ``ProfileDelta``: persistence moved by the change the profile expects
  between origin and target, capped by the capacity seen in training (or
  what the station holds now, if more); event
  probabilities are persistence calibrated on training examples.

Without a profile for a slot (never seen in training), profiles fall back on
persistence; ``fallback_share`` reports how often that happened in the last
``predict``.
"""

import polars as pl

from veloflow.evaluation.examples import TARGETS
from veloflow.models.base import KEYS, TrainingData

PARIS = "Europe/Paris"
SLOT_MINUTES = 15
PROFILE_KEYS: tuple[str, ...] = ("station_id", "day_type", "slot")
# Calibrated persistence groups the state at the origin as 0, 1, 2, 3 or more.
TOP_BUCKET = 3


def calendar_keys(time: pl.Expr) -> list[pl.Expr]:
    """``day_type`` (weekday or weekend) and ``slot`` (Paris minute of day, 15-minute steps)."""
    local = time.dt.convert_time_zone(PARIS)
    return [
        pl.when(local.dt.weekday() >= 6)
        .then(pl.lit("weekend"))
        .otherwise(pl.lit("weekday"))
        .alias("day_type"),
        (
            local.dt.hour().cast(pl.Int32) * 60
            + local.dt.minute().cast(pl.Int32) // SLOT_MINUTES * SLOT_MINUTES
        ).alias("slot"),
    ]


def _bikes(prefix: str = "") -> pl.Expr:
    return pl.col(f"{prefix}mechanical") + pl.col(f"{prefix}ebike")


def persistence_predictions(examples: pl.DataFrame) -> pl.DataFrame:
    """Forecast the state at the origin, with probabilities 0 or 1."""
    return examples.select(
        *KEYS,
        *[pl.col(name).cast(pl.Float64).alias(f"pred_{name}") for name in TARGETS],
        p_empty=(_bikes() == 0).cast(pl.Float64),
        p_full=(pl.col("docks") == 0).cast(pl.Float64),
    )


def station_profile(states: pl.DataFrame) -> pl.DataFrame:
    """Mean counts and event frequencies per station, type of day and slot."""
    return (
        states.with_columns(calendar_keys(pl.col("time")))
        .group_by(PROFILE_KEYS)
        .agg(
            *[pl.col(name).cast(pl.Float64).mean().alias(f"profile_{name}") for name in TARGETS],
            (_bikes() == 0).mean().alias("profile_empty"),
            (pl.col("docks") == 0).mean().alias("profile_full"),
        )
    )


class Persistence:
    """Nothing changes."""

    name = "persistence"

    def fit(self, data: TrainingData) -> None:
        """Nothing to learn."""

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """Forecast the state at the origin."""
        return persistence_predictions(examples)


class StationProfile:
    """Historical mean of the station, same type of day and slot as the target."""

    name = "profile"

    def __init__(self) -> None:
        """Start unfitted."""
        self.profile: pl.DataFrame | None = None
        self.fallback_share: float | None = None

    def fit(self, data: TrainingData) -> None:
        """Average the trustworthy training states."""
        self.profile = station_profile(data.states())

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """Profile at the target time; persistence where the slot was never seen."""
        if self.profile is None:
            raise RuntimeError("fit must be called before predict")
        joined = examples.with_columns(calendar_keys(pl.col("target_at"))).join(
            self.profile, on=list(PROFILE_KEYS), how="left"
        )
        fallback = persistence_predictions(joined)
        self.fallback_share = (
            joined["profile_mechanical"].null_count() / joined.height if joined.height else None
        )
        return joined.select(
            *KEYS,
            *[
                pl.coalesce(f"profile_{name}", fallback[f"pred_{name}"]).alias(f"pred_{name}")
                for name in TARGETS
            ],
            p_empty=pl.coalesce("profile_empty", fallback["p_empty"]),
            p_full=pl.coalesce("profile_full", fallback["p_full"]),
        )


class ProfileDelta:
    """Persistence plus the change the profile expects; calibrated event probabilities."""

    name = "persistence_profile_delta"

    def __init__(self) -> None:
        """Start unfitted."""
        self.profile: pl.DataFrame | None = None
        self.capacity: pl.DataFrame | None = None
        self.calibration: dict[str, pl.DataFrame] = {}
        self.fallback_share: float | None = None

    def fit(self, data: TrainingData) -> None:
        """Learn the profile, the capacity seen, and calibrated persistence."""
        states = data.states()
        self.profile = station_profile(states)
        self.capacity = states.group_by("station_id").agg(
            (_bikes() + pl.col("docks")).max().cast(pl.Float64).alias("capacity")
        )
        examples = data.examples()
        self.calibration = {
            "empty": examples.group_by(
                "horizon_min", _bikes().clip(upper_bound=TOP_BUCKET).alias("bucket")
            ).agg(pl.col("y_empty").mean().alias("calibrated")),
            "full": examples.group_by(
                "horizon_min", pl.col("docks").clip(upper_bound=TOP_BUCKET).alias("bucket")
            ).agg(pl.col("y_full").mean().alias("calibrated")),
        }

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """Forecast from the state at the origin and the expected change."""
        if self.profile is None or self.capacity is None:
            raise RuntimeError("fit must be called before predict")
        at_origin = self.profile.select(
            *PROFILE_KEYS, *[pl.col(f"profile_{name}").alias(f"origin_{name}") for name in TARGETS]
        )
        at_target = self.profile.select(
            *PROFILE_KEYS, *[pl.col(f"profile_{name}").alias(f"target_{name}") for name in TARGETS]
        )
        joined = (
            examples.with_columns(calendar_keys(pl.col("origin")))
            .join(at_origin, on=list(PROFILE_KEYS), how="left")
            .drop("day_type", "slot")
            .with_columns(calendar_keys(pl.col("target_at")))
            .join(at_target, on=list(PROFILE_KEYS), how="left")
            .join(self.capacity, on="station_id", how="left")
            .with_columns(
                empty_bucket=_bikes().clip(upper_bound=TOP_BUCKET),
                full_bucket=pl.col("docks").clip(upper_bound=TOP_BUCKET),
            )
            .join(
                self.calibration["empty"].rename({"calibrated": "calibrated_empty"}),
                left_on=["horizon_min", "empty_bucket"],
                right_on=["horizon_min", "bucket"],
                how="left",
            )
            .join(
                self.calibration["full"].rename({"calibrated": "calibrated_full"}),
                left_on=["horizon_min", "full_bucket"],
                right_on=["horizon_min", "bucket"],
                how="left",
            )
        )
        missing = pl.col("origin_mechanical").is_null() | pl.col("target_mechanical").is_null()
        self.fallback_share = joined.select(missing.mean()).item() if joined.height else None
        fallback = persistence_predictions(joined)

        def forecast(name: str) -> pl.Expr:
            delta = (pl.col(f"target_{name}") - pl.col(f"origin_{name}")).fill_null(0.0)
            raw = (pl.col(name).cast(pl.Float64) + delta).clip(lower_bound=0.0)
            # The cap is never below what the station holds now: some stations
            # hold more than ever seen in training (docs/data/quality.md, R2).
            # max_horizontal ignores a null capacity (station unseen in training).
            held = (_bikes() + pl.col("docks")).cast(pl.Float64)
            cap = pl.max_horizontal(pl.col("capacity"), held)
            return pl.min_horizontal(raw, cap).alias(f"pred_{name}")

        return joined.select(
            *KEYS,
            *[forecast(name) for name in TARGETS],
            p_empty=pl.coalesce("calibrated_empty", fallback["p_empty"]),
            p_full=pl.coalesce("calibrated_full", fallback["p_full"]),
        )
