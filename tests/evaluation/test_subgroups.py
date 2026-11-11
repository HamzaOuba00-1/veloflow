"""Tests for the subgroups of acceptance criterion 5."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from veloflow.evaluation.subgroups import add_subgroups, station_areas, subgroup_ratios

STATIONS = pl.DataFrame(
    {
        "station_id": [1, 2, 3],
        "station_code": ["4012", "92004", "60001"],  # Paris, Nanterre, Invalides
        "lat": [48.8566, 48.8950, 48.8620],
        "lon": [2.3522, 2.2070, 2.3140],
    }
)


def test_paris_stations_from_their_code() -> None:
    assert station_areas(STATIONS)["suburbs"].to_list() == [False, True, False]


def scored(origins: list[datetime], stations: list[int]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_id": stations,
            "origin": origins,
            "target_at": [t + timedelta(minutes=15) for t in origins],
            "horizon_min": [15] * len(origins),
        },
        schema_overrides={
            "origin": pl.Datetime("us", "UTC"),
            "target_at": pl.Datetime("us", "UTC"),
            "horizon_min": pl.Int16,
        },
    )


def test_peak_hours_weekend_and_rain() -> None:
    origins = [
        datetime(2026, 11, 10, 7, 30, tzinfo=UTC),  # Tuesday 08:30 Paris: peak
        datetime(2026, 11, 10, 11, 0, tzinfo=UTC),  # Tuesday 12:00: off-peak
        datetime(2026, 11, 14, 7, 30, tzinfo=UTC),  # Saturday 08:30: weekend
    ]
    weather = pl.DataFrame(
        {
            "point": ["C", "C"],
            "valid_at": [
                datetime(2026, 11, 10, 7, 45, tzinfo=UTC),
                datetime(2026, 11, 10, 11, 15, tzinfo=UTC),
            ],
            "precipitation": [0.4, 0.0],
        },
        schema_overrides={"valid_at": pl.Datetime("us", "UTC")},
    )

    flagged = add_subgroups(scored(origins, [1, 1, 1]), STATIONS, weather)

    assert flagged["peak_hours"].to_list() == [True, False, False]
    assert flagged["weekend"].to_list() == [False, False, True]
    # Rain at the first target, dry at the second, no forecast for the third.
    assert flagged["rain"].to_list() == [True, False, None]
    assert flagged["suburbs"].to_list() == [False, False, False]
    assert flagged["origin"].to_list() == origins


def test_no_weather_means_unknown_rain() -> None:
    origins = [datetime(2026, 11, 10, 7, 30, tzinfo=UTC)]

    assert add_subgroups(scored(origins, [2]), STATIONS, None)["rain"].to_list() == [None]


def test_ratios_overall_and_per_subgroup() -> None:
    frame = pl.DataFrame(
        {
            "horizon_min": [15, 15, 15],
            "suburbs": [False, True, True],
            "peak_hours": [False, False, False],
            "rain": [None, None, None],
            "weekend": [False, False, False],
            **{f"y_{n}": [1, 1, 1] for n in ("mechanical", "ebike", "docks")},
            # Persistence is 1 off on mechanical bikes everywhere.
            **{
                n: [2, 2, 2] if n == "mechanical" else [1, 1, 1]
                for n in ("mechanical", "ebike", "docks")
            },
            # The model is exact in Paris, 1 off in the suburbs.
            "pred_mechanical": [1.0, 2.0, 2.0],
            "pred_ebike": [1.0, 1.0, 1.0],
            "pred_docks": [1.0, 1.0, 1.0],
        }
    )

    ratios = subgroup_ratios(frame)

    rows = {
        (r["group"], r["horizon_min"]): (r["n"], r["ratio"]) for r in ratios.iter_rows(named=True)
    }
    assert rows[("all", 15)] == (3, pytest.approx(2 / 3))
    assert rows[("suburbs", 15)] == (2, pytest.approx(1.0))
    assert ("rain", 15) not in rows  # no example: no row, the check then reads n/a
