"""Tests for distances and nearest neighbours."""

import polars as pl
import pytest

from veloflow.geo import haversine_m, nearest_neighbors


def test_haversine_known_distance() -> None:
    # Notre-Dame to the Eiffel Tower: about 4.1 km.
    frame = pl.DataFrame({"a": [48.8530], "b": [2.3499], "c": [48.8584], "d": [2.2945]})

    distance = frame.select(haversine_m(pl.col("a"), pl.col("b"), pl.col("c"), pl.col("d"))).item()

    assert distance == pytest.approx(4_100, rel=0.03)


def test_haversine_same_point_is_zero() -> None:
    frame = pl.DataFrame({"lat": [48.85], "lon": [2.35]})

    assert frame.select(
        haversine_m(pl.col("lat"), pl.col("lon"), pl.col("lat"), pl.col("lon"))
    ).item() == pytest.approx(0.0)


STATIONS = pl.DataFrame(
    {
        "station_id": [1, 2, 3, 4],
        # On a line going east, steps exact in binary so that 2 is exactly
        # as far from 1 as from 3; 4 is far away.
        "lat": [48.85, 48.85, 48.85, 48.85],
        "lon": [2.25, 2.5, 2.75, 3.5],
    }
)


def test_neighbours_are_ranked_by_distance_without_self() -> None:
    result = nearest_neighbors(STATIONS, k=2)

    assert result.filter(pl.col("station_id") == 1)["neighbor_id"].to_list() == [2, 3]
    assert result.filter(pl.col("station_id") == 4)["neighbor_id"].to_list() == [3, 2]
    assert result.filter(pl.col("station_id") == 2)["rank"].to_list() == [1, 2]
    assert (result["station_id"] != result["neighbor_id"]).all()
    assert result.height == 8


def test_ties_are_broken_by_id_and_k_is_capped() -> None:
    # Station 2 is equidistant from 1 and 3.
    result = nearest_neighbors(STATIONS, k=10)

    assert result.filter(pl.col("station_id") == 2)["neighbor_id"].to_list() == [1, 3, 4]
    assert result.height == 12


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        nearest_neighbors(STATIONS, k=0)
