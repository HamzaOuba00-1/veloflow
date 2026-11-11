"""Rule R4: frozen data, for the whole feed (R4a) or for one station (R4b).

R4a. On 9 November 2026, from 13:45 to 16:00 Paris time, the feed kept
publishing a version a minute but only 190 to 500 stations changed per quarter
hour, against about 2,300 the next working day at the same time: night-time
activity in mid-afternoon, then a catch-up burst at 16:00. The coverage report
(R6) cannot see it, since versions do arrive. A quarter hour is flagged when
the changes fall below a share of what the same quarter hour brings on the
other days of the same type; the next quarter hour, the catch-up, is flagged
too.

R4b. A station can stay open, keep reporting to the operator, and never
change: "Saint-Germain - Verdun" showed 0 bikes and 30 free docks for 58.7
hours in a row. A station is suspected frozen once it has not changed during a
given number of daytime hours; "Lacépède - Monge" crossed the threshold about
a day before the operator closed it. The threshold is provisional: its error
rate will be measured on two weeks (docs/data/quality.md).

Both rules work on rows from ``veloflow.datasets.load_station_status``.
"""

import polars as pl

PARIS = "Europe/Paris"
STATE = ("mechanical", "ebike", "docks")


def change_counts(frame: pl.DataFrame) -> pl.DataFrame:
    """Number of operational stations whose state changed, per version.

    Returns:
        Columns ``fetched_at`` and ``changed``, ordered by time. The first
        version of a station is not counted as a change.
    """
    changed = pl.any_horizontal(
        [(pl.col(name) != pl.col(name).shift(1).over("station_id")) for name in STATE]
    )
    return (
        frame.filter(pl.col("operational"))
        .sort("station_id", "fetched_at")
        .with_columns(changed=changed.fill_null(value=False))
        .group_by("fetched_at")
        .agg(pl.col("changed").sum().cast(pl.Int64))
        .sort("fetched_at")
    )


def frozen_feed_slots(
    counts: pl.DataFrame,
    *,
    slot: str = "15m",
    threshold: float = 0.3,
    min_expected: float = 300.0,
    min_versions: int = 9,
) -> pl.DataFrame:
    """Rule R4a: quarter hours where the whole feed barely moves.

    The expected number of changes of a slot is the median of the same local
    time of day on the **other** days of the same type (working day or
    weekend), so an incident never lowers its own reference.

    Args:
        counts: output of ``change_counts``.
        slot: slot length, as a Polars duration.
        threshold: a slot is frozen below this share of the expected changes.
        min_expected: slots expected to be calmer than this (nights) are never
            flagged: a quiet night is not an incident.
        min_versions: slots with fewer versions are a coverage gap (rule R6),
            not a frozen feed.

    Returns:
        One row per flagged slot: ``slot_start`` (UTC), ``changed``,
        ``expected``, ``reason`` (``"frozen"``, or ``"catch_up"`` for the slot
        that follows a frozen period). A slot just above the threshold between
        two frozen slots is part of the same incident.
    """
    step = _minutes(slot)
    slots = (
        counts.group_by(pl.col("fetched_at").dt.truncate(slot).alias("slot_start"))
        .agg(pl.col("changed").sum(), pl.len().alias("versions"))
        .with_columns(local=pl.col("slot_start").dt.convert_time_zone(PARIS))
        .with_columns(
            day=pl.col("local").dt.date(),
            time_of_day=pl.col("local").dt.strftime("%H:%M"),
            weekend=pl.col("local").dt.weekday() >= 6,
        )
        .sort("slot_start")
    )
    others = slots.select(
        "time_of_day", "weekend", pl.col("day").alias("other_day"), pl.col("changed").alias("other")
    )
    expected = (
        slots.select("slot_start", "day", "time_of_day", "weekend")
        .join(others, on=["time_of_day", "weekend"])
        .filter(pl.col("other_day") != pl.col("day"))
        .group_by("slot_start")
        .agg(pl.col("other").median().alias("expected"))
    )
    flagged = (
        slots.join(expected, on="slot_start", how="left")
        .with_columns(
            low=(
                (pl.col("expected") >= min_expected)
                & (pl.col("versions") >= min_versions)
                & (pl.col("changed") < threshold * pl.col("expected"))
            ).fill_null(value=False),
            follows=pl.col("slot_start").diff() == pl.duration(minutes=step),
            precedes=pl.col("slot_start").diff(-1) == -pl.duration(minutes=step),
        )
        .with_columns(
            # A slot just above the threshold between two frozen slots belongs
            # to the same incident.
            frozen=pl.col("low")
            | (
                pl.col("low").shift(1).fill_null(value=False)
                & pl.col("low").shift(-1).fill_null(value=False)
                & pl.col("follows")
                & pl.col("precedes")
            )
        )
        .with_columns(
            catch_up=~pl.col("frozen")
            & pl.col("frozen").shift(1).fill_null(value=False)
            & pl.col("follows").fill_null(value=False)
        )
    )
    return (
        flagged.filter(pl.col("frozen") | pl.col("catch_up"))
        .with_columns(
            reason=pl.when(pl.col("frozen")).then(pl.lit("frozen")).otherwise(pl.lit("catch_up"))
        )
        .select("slot_start", "changed", "expected", "reason")
    )


def _minutes(duration: str) -> int:
    """Minutes of a duration written ``"15m"`` or ``"1h"``."""
    value, unit = int(duration[:-1]), duration[-1]
    if unit == "m":
        return value
    if unit == "h":
        return 60 * value
    raise ValueError(f"unsupported duration {duration!r}")


def suspected_frozen_stations(
    frame: pl.DataFrame,
    *,
    min_daytime_hours: float = 12.0,
    day_start_hour: int = 7,
    day_end_hour: int = 22,
) -> pl.DataFrame:
    """Rule R4b: periods during which a station is suspected frozen.

    A run is a sequence of versions where an operational station keeps the
    same state. Its daytime duration (Paris time, ``day_start_hour`` to
    ``day_end_hour``) accumulates from the start of the run; the station is
    suspected frozen from the version where it reaches ``min_daytime_hours``
    until the run ends (next change, or end of data).

    Returns:
        Columns ``station_id``, ``frozen_from``, ``frozen_until`` (UTC) and
        ``daytime_hours`` (daytime length of the whole run).
    """
    stations = (
        frame.filter(pl.col("operational"))
        .sort("station_id", "fetched_at")
        .with_columns(
            changed=pl.any_horizontal(
                [(pl.col(name) != pl.col(name).shift(1).over("station_id")) for name in STATE]
            ).fill_null(value=True),
            local_hour=pl.col("fetched_at").dt.convert_time_zone(PARIS).dt.hour(),
            gap_h=(pl.col("fetched_at").shift(-1).over("station_id") - pl.col("fetched_at"))
            .dt.total_seconds()
            .fill_null(0)
            / 3600,
        )
        .with_columns(
            run=pl.col("changed").cast(pl.Int32).cum_sum().over("station_id"),
            daytime_gap_h=pl.when(pl.col("local_hour").is_between(day_start_hour, day_end_hour - 1))
            .then(pl.col("gap_h"))
            .otherwise(0.0),
        )
        .with_columns(
            # Daytime hours elapsed before this version, within the run.
            elapsed_h=(
                pl.col("daytime_gap_h").cum_sum().over("station_id", "run")
                - pl.col("daytime_gap_h")
            )
        )
    )
    runs = stations.group_by("station_id", "run").agg(
        pl.col("fetched_at")
        .filter(pl.col("elapsed_h") >= min_daytime_hours)
        .min()
        .alias("frozen_from"),
        pl.col("fetched_at").max().alias("last_version"),
        pl.col("daytime_gap_h").sum().alias("daytime_hours"),
    )
    next_change = stations.filter(pl.col("changed")).select(
        "station_id", (pl.col("run") - 1).alias("run"), pl.col("fetched_at").alias("next_change")
    )
    return (
        runs.filter(pl.col("frozen_from").is_not_null())
        .join(next_change, on=["station_id", "run"], how="left")
        .select(
            "station_id",
            "frozen_from",
            pl.coalesce("next_change", "last_version").alias("frozen_until"),
            pl.col("daytime_hours").round(2),
        )
        .sort("station_id", "frozen_from")
    )
