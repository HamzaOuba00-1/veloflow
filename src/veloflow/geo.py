"""Distances between stations and their nearest neighbours.

Stations close to each other share their users: when one runs out of bikes,
riders walk to the next one. Neighbour relations are therefore a candidate
feature (docs/data/eda.md, step 3.3) and the edges of a station graph.
"""

import polars as pl

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: pl.Expr, lon1: pl.Expr, lat2: pl.Expr, lon2: pl.Expr) -> pl.Expr:
    """Great-circle distance in metres between two points given in degrees."""
    phi1, phi2 = lat1.radians(), lat2.radians()
    half_dphi = (lat2 - lat1).radians() / 2
    half_dlambda = (lon2 - lon1).radians() / 2
    a = half_dphi.sin().pow(2) + phi1.cos() * phi2.cos() * half_dlambda.sin().pow(2)
    return 2 * EARTH_RADIUS_M * a.sqrt().arcsin()


def nearest_neighbors(stations: pl.DataFrame, k: int) -> pl.DataFrame:
    """The ``k`` nearest other stations of every station.

    Args:
        stations: columns ``station_id``, ``lat`` and ``lon``.
        k: neighbours per station (fewer if there are not enough stations).

    Returns:
        Columns ``station_id``, ``neighbor_id``, ``rank`` (1 = nearest) and
        ``distance_m``. Ties are broken by neighbour id, so the result is
        deterministic.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    points = stations.select("station_id", "lat", "lon")
    others = points.rename({"station_id": "neighbor_id", "lat": "lat_n", "lon": "lon_n"})
    return (
        points.join(others, how="cross")
        .filter(pl.col("station_id") != pl.col("neighbor_id"))
        .with_columns(
            distance_m=haversine_m(pl.col("lat"), pl.col("lon"), pl.col("lat_n"), pl.col("lon_n"))
        )
        .sort("station_id", "distance_m", "neighbor_id")
        .with_columns(rank=pl.int_range(1, pl.len() + 1).over("station_id").cast(pl.UInt32))
        .filter(pl.col("rank") <= k)
        .select("station_id", "neighbor_id", "rank", "distance_m")
    )
