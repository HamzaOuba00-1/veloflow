"""Fixed grid of weather points covering the Vélib' network.

Rain varies within the metropolitan area: over the first week of November
2026, five points of the area disagreed on "raining or not" for 3 to 11 hours
out of the week (docs/data/weather.md). A single point for 1,520 stations would
blur that, so the area is split into a 3 x 3 grid and every station takes the
weather of its nearest point.

The points are frozen constants, not recomputed from the current stations, so
that weather series keep the same locations for the whole project.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GridPoint:
    """One location where the weather is requested."""

    code: str
    lat: float
    lon: float


# Cell centres of a 3 x 3 split of the bounding box of the 1,520 stations
# recorded on 4 November 2026 (lat 48.743-48.958, lon 2.166-2.538, about
# 24 km x 27 km), rounded to 3 decimals (~100 m). Every cell holds stations.
GRID: tuple[GridPoint, ...] = (
    GridPoint("SW", 48.778, 2.228),
    GridPoint("S", 48.778, 2.352),
    GridPoint("SE", 48.778, 2.476),
    GridPoint("W", 48.849, 2.228),
    GridPoint("C", 48.849, 2.352),
    GridPoint("E", 48.849, 2.476),
    GridPoint("NW", 48.921, 2.228),
    GridPoint("N", 48.921, 2.352),
    GridPoint("NE", 48.921, 2.476),
)

# A degree of longitude is shorter than a degree of latitude away from the
# equator; at Paris the ratio is cos(48.85°) ≈ 0.66.
_LON_SCALE = math.cos(math.radians(48.85))


def nearest_point(lat: float, lon: float, grid: Sequence[GridPoint] = GRID) -> GridPoint:
    """Return the grid point closest to (``lat``, ``lon``).

    Uses an equirectangular approximation: over 30 km its error is far below
    the 8 km spacing of the grid, and it needs no dependency.

    Raises:
        ValueError: ``grid`` is empty.
    """
    if not grid:
        raise ValueError("the grid has no point")
    return min(
        grid,
        key=lambda point: (point.lat - lat) ** 2 + ((point.lon - lon) * _LON_SCALE) ** 2,
    )
