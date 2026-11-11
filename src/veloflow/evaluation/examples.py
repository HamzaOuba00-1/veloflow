"""Evaluation examples: what a forecaster knew, and what happened next.

An example is a station, an origin time ``t`` (when the forecast is made) and
a horizon ``h``. It holds the state known at ``t`` and the state observed at
``t + h``, read from the same regular grid (``datasets.resample_status``): at
both times, the last version fetched at or before that time, at most 5
minutes old. A model is evaluated on the targets only.

An example is kept only if both ends are trustworthy (docs/data/quality.md):

* the state is known (a recent version exists);
* the station is operational (rule R3);
* the feed is not frozen and not catching up (rule R4a).

Suspected frozen stations (rule R4b) are kept and flagged, so that results can
be reported with and without them.
"""

from dataclasses import dataclass

import polars as pl

HORIZONS_MIN: tuple[int, ...] = (15, 30, 60)
TARGETS: tuple[str, ...] = ("mechanical", "ebike", "docks")

# Reasons are checked in this order; an example is counted under the first one.
EXCLUSION_REASONS: tuple[str, ...] = (
    "origin_unknown",
    "origin_not_operational",
    "origin_frozen_feed",
    "target_unknown",
    "target_not_operational",
    "target_frozen_feed",
)


@dataclass(frozen=True, slots=True)
class ExampleSet:
    """Kept examples, and how many candidates each rule removed."""

    frame: pl.DataFrame
    candidates: int
    excluded: dict[str, int]


def _flag_grid(
    grid: pl.DataFrame,
    frozen_slots: pl.DataFrame | None,
    frozen_stations: pl.DataFrame | None,
    slot: str,
) -> pl.DataFrame:
    """Per grid row: state, and whether it is known, operational, frozen."""
    flags = grid.select(
        "station_id",
        "time",
        *TARGETS,
        known=pl.col("fetched_at").is_not_null(),
        operational=pl.col("operational").fill_null(value=False),
    )
    if frozen_slots is not None and frozen_slots.height > 0:
        slots = (
            frozen_slots.select("slot_start").unique().with_columns(frozen_feed=pl.lit(value=True))
        )
        flags = flags.join(
            slots, left_on=pl.col("time").dt.truncate(slot), right_on="slot_start", how="left"
        )
        flags = flags.with_columns(pl.col("frozen_feed").fill_null(value=False))
    else:
        flags = flags.with_columns(frozen_feed=pl.lit(value=False))

    flags = flags.with_columns(suspected=pl.lit(value=False))
    if frozen_stations is not None and frozen_stations.height > 0:
        periods = frozen_stations.select("station_id", "frozen_from", "frozen_until")
        # Only the few flagged stations are joined; a station may have
        # several periods, hence the aggregation.
        suspected = (
            flags.select("station_id", "time")
            .join(periods, on="station_id")
            .filter(pl.col("time").is_between("frozen_from", "frozen_until", closed="left"))
            .select("station_id", "time")
            .unique()
            .with_columns(hit=pl.lit(value=True))
        )
        flags = (
            flags.join(suspected, on=["station_id", "time"], how="left")
            .with_columns(suspected=pl.col("hit").fill_null(value=False))
            .drop("hit")
        )
    return flags


def build_examples(
    grid: pl.DataFrame,
    *,
    horizons: tuple[int, ...] = HORIZONS_MIN,
    frozen_slots: pl.DataFrame | None = None,
    frozen_stations: pl.DataFrame | None = None,
    slot: str = "15m",
) -> ExampleSet:
    """Build the evaluation examples of a regular grid.

    Args:
        grid: output of ``datasets.resample_status``.
        horizons: forecast horizons in minutes, multiples of the grid step.
        frozen_slots: output of ``quality.freeze.frozen_feed_slots`` (R4a).
        frozen_stations: output of ``quality.freeze.suspected_frozen_stations``
            (R4b).
        slot: slot length used to compute ``frozen_slots``.

    Returns:
        Kept examples with columns ``station_id``, ``origin``, ``horizon_min``,
        ``target_at``, the state at the origin (``mechanical``, ``ebike``,
        ``docks``), the targets (``y_mechanical``, ``y_ebike``, ``y_docks``,
        ``y_empty`` = no bike at all, ``y_full`` = no free dock) and
        ``suspected_frozen`` (R4b at the origin or at the target). Candidates
        are the station-origin-horizon triples whose target time is on the
        grid.
    """
    flags = _flag_grid(grid, frozen_slots, frozen_stations, slot)
    origin = flags.rename({"time": "origin"})
    target = flags.select(
        "station_id",
        pl.col("time").alias("target_at"),
        *[pl.col(name).alias(f"y_{name}") for name in TARGETS],
        pl.col("known").alias("t_known"),
        pl.col("operational").alias("t_operational"),
        pl.col("frozen_feed").alias("t_frozen_feed"),
        pl.col("suspected").alias("t_suspected"),
    )
    reason = (
        pl.when(~pl.col("known"))
        .then(pl.lit("origin_unknown"))
        .when(~pl.col("operational"))
        .then(pl.lit("origin_not_operational"))
        .when(pl.col("frozen_feed"))
        .then(pl.lit("origin_frozen_feed"))
        .when(~pl.col("t_known"))
        .then(pl.lit("target_unknown"))
        .when(~pl.col("t_operational"))
        .then(pl.lit("target_not_operational"))
        .when(pl.col("t_frozen_feed"))
        .then(pl.lit("target_frozen_feed"))
    )
    parts = []
    for horizon in horizons:
        parts.append(
            origin.with_columns(
                horizon_min=pl.lit(horizon, dtype=pl.Int16),
                target_at=pl.col("origin") + pl.duration(minutes=horizon),
            )
            # Inner join: candidates are the examples whose target is on the grid.
            .join(target, on=["station_id", "target_at"])
            .with_columns(reason=reason)
        )
    candidates = pl.concat(parts)
    counts = dict(
        candidates.filter(pl.col("reason").is_not_null()).group_by("reason").len().iter_rows()
    )
    frame = (
        candidates.filter(pl.col("reason").is_null())
        .select(
            "station_id",
            "origin",
            "horizon_min",
            "target_at",
            *TARGETS,
            *[f"y_{name}" for name in TARGETS],
            y_empty=(pl.col("y_mechanical") + pl.col("y_ebike")) == 0,
            y_full=pl.col("y_docks") == 0,
            suspected_frozen=pl.col("suspected") | pl.col("t_suspected"),
        )
        .sort("horizon_min", "station_id", "origin")
    )
    return ExampleSet(
        frame=frame,
        candidates=candidates.height,
        excluded={name: int(counts.get(name, 0)) for name in EXCLUSION_REASONS},
    )
