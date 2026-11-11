"""Tests for the features, on the synthetic status of tests/conftest.py.

Version ``k`` is fetched at 8 November 2026 12:00 UTC + 5k minutes. Station 1
holds ``k % 5`` mechanical bikes and no e-bike; station 2 holds ``1 + k % 3``
mechanical bikes and ``k % 2`` e-bikes.
"""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from veloflow.features.build import (
    FEATURE_COLUMNS,
    build_features,
    make_context,
    station_states,
)

START = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
STATIONS = pl.DataFrame(
    {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
    schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
)


def at(k: int) -> datetime:
    return START + timedelta(minutes=5 * k)


def examples(status: pl.DataFrame, origins: list[datetime], horizon: int = 15) -> pl.DataFrame:
    """Inputs of station 1 at ``origins``, with the state of that version."""
    return (
        status.filter(pl.col("station_id") == 1, pl.col("fetched_at").is_in(origins))
        .select(
            "station_id",
            pl.col("fetched_at").alias("origin"),
            pl.lit(horizon, dtype=pl.Int16).alias("horizon_min"),
            (pl.col("fetched_at") + pl.duration(minutes=horizon)).alias("target_at"),
            "mechanical",
            "ebike",
            "docks",
        )
        .sort("origin")
    )


def test_state_and_recent_changes(synthetic_status: pl.DataFrame) -> None:
    features = build_features(
        examples(synthetic_status, [at(100)]), synthetic_status, make_context(STATIONS)
    )

    row = features.row(0, named=True)
    # k = 100: 0 bikes; 15, 30, 60 minutes earlier (k = 97, 94, 88): 2, 4, 3.
    assert (row["mechanical"], row["bikes"], row["total"]) == (0.0, 0.0, 10.0)
    assert row["fill"] == 0.0
    assert (row["d_mechanical_15"], row["d_mechanical_30"], row["d_mechanical_60"]) == (
        -2.0,
        -4.0,
        -3.0,
    )
    assert row["d_docks_15"] == 2.0


def test_neighbors_at_the_origin(synthetic_status: pl.DataFrame) -> None:
    features = build_features(
        examples(synthetic_status, [at(100)]), synthetic_status, make_context(STATIONS)
    )

    row = features.row(0, named=True)
    # Station 2 at k = 100: 2 mechanical, 0 e-bike, docks (k + 1) % 4 = 1.
    assert (row["nb_bikes"], row["nb_docks"], row["nb_empty"], row["nb_full"]) == (2, 1, 0, 0)
    assert row["nb_fill"] == pytest.approx(2 / 3)
    # 15 minutes earlier (k = 97): 1 + 1 mechanical and 1 e-bike, so -1.
    assert row["nb_d_bikes_15"] == -1.0
    assert 0 < row["nb_distance_m"] < 100


def test_calendar_of_the_target(synthetic_status: pl.DataFrame) -> None:
    # Origin 10 November 23:30 UTC is 00:30 on 11 November in Paris: the
    # target falls on Armistice Day, a public holiday.
    k = int((datetime(2026, 11, 10, 23, 30, tzinfo=UTC) - START).total_seconds() // 300)
    features = build_features(
        examples(synthetic_status, [at(k)]), synthetic_status, make_context(STATIONS)
    )

    row = features.row(0, named=True)
    assert (row["origin_hour"], row["target_hour"]) == (0.5, 0.75)
    assert row["is_public_holiday"] == 1.0
    assert row["weekday"] == 2.0  # Wednesday
    assert row["is_weekend"] == 0.0


def test_weather_forecast_at_the_target(synthetic_status: pl.DataFrame) -> None:
    weather = pl.DataFrame(
        {
            "point": ["C", "C", "N"],
            "valid_at": [at(100), at(103), at(103)],
            "temperature_2m": [8.0, 9.5, 1.0],
            "precipitation": [0.0, 0.4, 5.0],
            "wind_speed_10m": [10.0, 12.0, 30.0],
        },
        schema_overrides={"valid_at": pl.Datetime("us", "UTC")},
    )
    context = make_context(STATIONS, weather)

    features = build_features(
        examples(synthetic_status, [at(100), at(200)]), synthetic_status, context
    )

    # Both stations sit in central Paris (point C). Target of k = 100 is
    # k = 103; nothing within 15 minutes of the target of k = 200.
    assert features["temperature_2m"].to_list() == [9.5, None]
    assert features["precipitation"].to_list()[0] == pytest.approx(0.4)


def test_unknown_past_is_null(synthetic_status: pl.DataFrame) -> None:
    # k = 2 is 10 minutes after the first version: no state 15 minutes earlier.
    features = build_features(
        examples(synthetic_status, [at(2)]), synthetic_status, make_context(STATIONS)
    )

    assert features["d_mechanical_15"].to_list() == [None]
    assert features["mechanical"].to_list() == [2.0]


def test_features_never_read_after_the_origin(synthetic_status: pl.DataFrame) -> None:
    origins = [at(k) for k in range(100, 160, 3)]
    cut = at(130)
    context = make_context(STATIONS)
    given = examples(synthetic_status, origins)

    full = build_features(given, synthetic_status, context).filter(pl.col("origin") <= cut)
    cut_off = build_features(
        given.filter(pl.col("origin") <= cut),
        synthetic_status.filter(pl.col("fetched_at") <= cut),
        context,
    )

    assert full.equals(cut_off)


def test_order_and_columns_are_kept(synthetic_status: pl.DataFrame) -> None:
    given = examples(synthetic_status, [at(150), at(100), at(120)]).reverse()

    features = build_features(given, synthetic_status, make_context(STATIONS))

    assert features["origin"].to_list() == given["origin"].to_list()
    assert features.columns == ["station_id", "origin", "horizon_min", *FEATURE_COLUMNS[1:]]
    assert build_features(given.head(0), synthetic_status, make_context(STATIONS)).height == 0


def test_no_origin_no_state(synthetic_status: pl.DataFrame) -> None:
    origins = pl.Series("origin", [], dtype=pl.Datetime("us", "UTC"))

    assert station_states(synthetic_status, origins).height == 0
