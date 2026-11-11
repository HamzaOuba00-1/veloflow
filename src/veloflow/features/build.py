"""Features of an example, using only what is known at its origin.

For an example (station, origin ``t``, horizon ``h``), every feature comes
from one of three sources:

* the station history up to ``t``: state, and its change over the last 15,
  30 and 60 minutes, read on the 5-minute grid (never a version after ``t``);
* the 5 nearest stations at ``t``: their mean state and recent change, and how
  many are empty or full (the exploration found the neighbours announce a
  station's next hour better than the station itself, docs/data/eda.md);
* context known in advance for ``t + h``: Paris time and calendar (weekday,
  public and school holidays), and the weather *forecast* for the station's
  grid point (Open-Meteo, decision D23).

The bench's look-ahead check covers this module through the models; a test
here also checks it directly.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import polars as pl

from veloflow.context.calendar import daily_calendar
from veloflow.datasets import resample_status
from veloflow.geo import nearest_neighbors
from veloflow.weather.grid import nearest_point

PARIS = "Europe/Paris"
LAGS_MIN: tuple[int, ...] = (15, 30, 60)
NEIGHBORS = 5
NEIGHBOR_LAG_MIN = 15
WEATHER_TOLERANCE = "15m"
WEATHER_COLUMNS: tuple[str, ...] = ("temperature_2m", "precipitation", "wind_speed_10m")
COUNTS: tuple[str, ...] = ("mechanical", "ebike", "docks")

FEATURE_COLUMNS: tuple[str, ...] = (
    "horizon_min",
    *COUNTS,
    "bikes",
    "total",
    "fill",
    *[f"d_{name}_{lag}" for lag in LAGS_MIN for name in COUNTS],
    "nb_bikes",
    "nb_docks",
    "nb_fill",
    "nb_empty",
    "nb_full",
    f"nb_d_bikes_{NEIGHBOR_LAG_MIN}",
    "nb_distance_m",
    "origin_hour",
    "target_hour",
    "weekday",
    "is_weekend",
    "is_public_holiday",
    "is_school_holiday",
    *WEATHER_COLUMNS,
)


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Static inputs: station neighbourhoods, weather points, forecasts."""

    neighbors: pl.DataFrame
    points: pl.DataFrame
    weather: pl.DataFrame | None


def make_context(stations: pl.DataFrame, weather: pl.DataFrame | None = None) -> FeatureContext:
    """Prepare the static inputs.

    Args:
        stations: ``datasets.load_station_information`` (``station_id``,
            ``lat``, ``lon``).
        weather: silver ``weather_15min`` rows (``point``, ``valid_at`` and
            ``WEATHER_COLUMNS``), or None.
    """
    points = pl.DataFrame(
        {
            "station_id": stations["station_id"],
            "point": [
                nearest_point(lat, lon).code for lat, lon in stations.select("lat", "lon").rows()
            ],
        },
        schema={"station_id": pl.Int64, "point": pl.String},
    )
    return FeatureContext(nearest_neighbors(stations, NEIGHBORS), points, weather)


def _calendar(days: list[date]) -> pl.DataFrame:
    """Calendar of the target dates (never empty: called with examples)."""
    return daily_calendar(min(days), max(days)).select(
        "date", "weekday", "is_weekend", "is_public_holiday", "is_school_holiday"
    )


def _local_hour(time: pl.Expr) -> pl.Expr:
    local = time.dt.convert_time_zone(PARIS)
    return local.dt.hour().cast(pl.Float32) + local.dt.minute().cast(pl.Float32) / 60


def station_states(history: pl.DataFrame, origins: pl.Series) -> pl.DataFrame:
    """Grid states and recent changes of every station, at every origin time.

    Only versions fetched at or before each origin are used: the window ends
    at the last origin, and the grid is an as-of join backwards.
    """
    if origins.len() == 0:
        return pl.DataFrame()
    first, last = (
        origins.to_frame("t").select(pl.col("t").min(), pl.col("t").max().alias("u")).row(0)
    )
    lag = timedelta(minutes=max(LAGS_MIN) + 5)
    window = history.filter(pl.col("fetched_at").is_between(first - lag, last))
    grid = resample_status(window, end=last).select("station_id", "time", *COUNTS)
    states = grid.filter(pl.col("time").is_in(origins.unique().implode()))
    for minutes in LAGS_MIN:
        past = grid.select(
            "station_id",
            (pl.col("time") + pl.duration(minutes=minutes)).alias("time"),
            *[pl.col(name).alias(f"past_{name}") for name in COUNTS],
        )
        states = (
            states.join(past, on=["station_id", "time"], how="left")
            .with_columns(
                *[
                    (pl.col(name) - pl.col(f"past_{name}")).alias(f"d_{name}_{minutes}")
                    for name in COUNTS
                ]
            )
            .drop([f"past_{name}" for name in COUNTS])
        )
    return states


def neighbor_features(states: pl.DataFrame, neighbors: pl.DataFrame) -> pl.DataFrame:
    """Mean state and recent change of each station's nearest neighbours."""
    bikes = pl.col("mechanical") + pl.col("ebike")
    known = states.filter(pl.col("mechanical").is_not_null()).select(
        pl.col("station_id").alias("neighbor_id"),
        "time",
        bikes.alias("bikes"),
        pl.col("docks"),
        (pl.col(f"d_mechanical_{NEIGHBOR_LAG_MIN}") + pl.col(f"d_ebike_{NEIGHBOR_LAG_MIN}")).alias(
            "d_bikes"
        ),
    )
    return (
        neighbors.join(known, on="neighbor_id")
        .group_by("station_id", "time")
        .agg(
            pl.col("bikes").mean().alias("nb_bikes"),
            pl.col("docks").mean().alias("nb_docks"),
            (pl.col("bikes").sum() / (pl.col("bikes") + pl.col("docks")).sum()).alias("nb_fill"),
            (pl.col("bikes") == 0).sum().alias("nb_empty"),
            (pl.col("docks") == 0).sum().alias("nb_full"),
            pl.col("d_bikes").mean().alias(f"nb_d_bikes_{NEIGHBOR_LAG_MIN}"),
            pl.col("distance_m").mean().alias("nb_distance_m"),
        )
    )


def build_features(
    examples: pl.DataFrame, history: pl.DataFrame, context: FeatureContext
) -> pl.DataFrame:
    """Features of ``examples``, from ``history`` up to each origin.

    Args:
        examples: ``models.base.INPUT_COLUMNS``.
        history: cleaned ``station_status`` rows.
        context: output of ``make_context``.

    Returns:
        ``station_id``, ``origin``, ``horizon_min`` and ``FEATURE_COLUMNS``,
        one row per example, in the order of ``examples``. Unknown values are
        null (gradient boosting handles them).
    """
    keys = ["station_id", "origin", "horizon_min"]
    if examples.height == 0:
        return examples.select(keys).with_columns(
            [pl.lit(None, dtype=pl.Float32).alias(name) for name in FEATURE_COLUMNS[1:]]
        )
    states = station_states(history, examples["origin"])
    lag_columns = [f"d_{name}_{lag}" for lag in LAGS_MIN for name in COUNTS]
    frame = examples.with_row_index("_row").join(
        states.select("station_id", pl.col("time").alias("origin"), *lag_columns),
        on=["station_id", "origin"],
        how="left",
    )
    frame = frame.join(
        neighbor_features(states, context.neighbors).rename({"time": "origin"}),
        on=["station_id", "origin"],
        how="left",
    )
    local_target = pl.col("target_at").dt.convert_time_zone(PARIS)
    frame = frame.with_columns(date=local_target.dt.date())
    frame = frame.join(_calendar(frame["date"].unique().to_list()), on="date", how="left")
    if context.weather is not None and context.weather.height > 0:
        weather = context.weather.select("point", "valid_at", *WEATHER_COLUMNS).sort("valid_at")
        frame = (
            frame.join(context.points, on="station_id", how="left")
            .sort("target_at")
            .join_asof(
                weather,
                left_on="target_at",
                right_on="valid_at",
                by="point",
                strategy="backward",
                tolerance=WEATHER_TOLERANCE,
                check_sortedness=False,
            )
        )
    else:
        frame = frame.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(c) for c in WEATHER_COLUMNS]
        )
    bikes = pl.col("mechanical") + pl.col("ebike")
    total = bikes + pl.col("docks")
    return (
        frame.with_columns(
            bikes=bikes,
            total=total,
            fill=pl.when(total > 0).then(bikes / total),
            origin_hour=_local_hour(pl.col("origin")),
            target_hour=_local_hour(pl.col("target_at")),
        )
        .sort("_row")
        .select(
            *keys,
            *[pl.col(name).cast(pl.Float32) for name in FEATURE_COLUMNS if name != "horizon_min"],
        )
    )
