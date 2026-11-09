"""Tests for the weather grid."""

import pytest

from veloflow.weather.grid import GRID, GridPoint, nearest_point


def test_grid_has_nine_distinct_points() -> None:
    assert len(GRID) == 9
    assert len({point.code for point in GRID}) == 9
    assert len({(point.lat, point.lon) for point in GRID}) == 9


@pytest.mark.parametrize("point", GRID, ids=lambda point: point.code)
def test_each_point_is_its_own_nearest(point: GridPoint) -> None:
    assert nearest_point(point.lat, point.lon) == point


@pytest.mark.parametrize(
    ("lat", "lon", "code"),
    [
        (48.8566, 2.3522, "C"),  # Hôtel de Ville
        (48.9362, 2.3574, "N"),  # Saint-Denis
        (48.7876, 2.3632, "S"),  # Villejuif
        (48.8352, 2.2410, "W"),  # Boulogne-Billancourt
        (48.8475, 2.4396, "E"),  # Vincennes
    ],
)
def test_towns_map_to_the_expected_point(lat: float, lon: float, code: str) -> None:
    assert nearest_point(lat, lon).code == code


def test_longitude_is_scaled_at_paris_latitude() -> None:
    # 0.06° east is ~4.4 km, 0.05° north is ~5.6 km: the eastern point is closer
    # although its raw degree difference is larger.
    north = GridPoint("north", 48.90, 2.30)
    east = GridPoint("east", 48.85, 2.36)
    assert nearest_point(48.85, 2.30, [north, east]) == east


def test_empty_grid_is_refused() -> None:
    with pytest.raises(ValueError, match="no point"):
        nearest_point(48.85, 2.35, [])
