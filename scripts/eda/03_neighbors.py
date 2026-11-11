"""Exploration 3.3: do nearby stations move together, and does that help predict?

Measured on the 5-minute grid of operational stations:

1. how far apart stations are (nearest neighbour distances);
2. whether the 15-minute change of a station follows its 5 nearest neighbours
   more than 5 random distant stations (co-movement);
3. whether the neighbours' recent change says anything about a station's
   change over the next 60 minutes, compared with its own recent change;
4. whether an empty neighbour announces the empty stations that persistence
   does not see coming (lift on the "unforeseen" events of step 3.2).

Exploratory script: loading, resampling and neighbour search live in the
package (``veloflow.datasets``, ``veloflow.geo``). Figures go to ``data/eda/``.

Usage:
    uv run python scripts/eda/03_neighbors.py --data-dir data --start 2026-11-08
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import polars as pl

from veloflow.datasets import load_station_information, load_station_status, resample_status
from veloflow.geo import nearest_neighbors

STEP = 5  # minutes
K = 5
FAR_M = 3_000.0


def build_grid(data_dir: Path, start: date | None, end: date | None) -> pl.DataFrame:
    """Grid of bikes per station with past and future changes."""
    data = load_station_status(data_dir, start, end)
    grid = resample_status(data.frame, every=f"{STEP}m", tolerance=f"{STEP}m")
    bikes = pl.col("mechanical").cast(pl.Int32) + pl.col("ebike")
    return (
        grid.with_columns(bikes=pl.when(pl.col("operational")).then(bikes))
        .with_columns(
            past=(pl.col("bikes") - pl.col("bikes").shift(15 // STEP)).over("station_id"),
            future=(pl.col("bikes").shift(-60 // STEP) - pl.col("bikes")).over("station_id"),
            empty_now=pl.col("bikes") == 0,
            empty_later=(pl.col("bikes").shift(-60 // STEP) == 0).over("station_id"),
        )
        .select("station_id", "time", "bikes", "past", "future", "empty_now", "empty_later")
    )


def neighbour_aggregates(grid: pl.DataFrame, pairs: pl.DataFrame, prefix: str) -> pl.DataFrame:
    """Mean past change and 'any empty' over the paired stations, per station and time."""
    other = grid.select(
        pl.col("station_id").alias("neighbor_id"),
        "time",
        pl.col("past").alias("n_past"),
        pl.col("empty_now").alias("n_empty"),
    )
    return (
        pairs.select("station_id", "neighbor_id")
        .join(other, on="neighbor_id")
        .group_by("station_id", "time")
        .agg(
            pl.col("n_past").mean().alias(f"{prefix}_past"),
            pl.col("n_empty").any().alias(f"{prefix}_any_empty"),
        )
    )


def random_far_pairs(info: pl.DataFrame, k: int, seed: int = 7) -> pl.DataFrame:
    """For each station, ``k`` random stations at least ``FAR_M`` away (control group)."""
    every = nearest_neighbors(info, k=info.height)
    return (
        every.filter(pl.col("distance_m") >= FAR_M)
        .with_columns(order=pl.int_range(pl.len()).shuffle(seed=seed).over("station_id"))
        .filter(pl.col("order") < k)
    )


def correlation(frame: pl.DataFrame, a: str, b: str) -> float:
    """Pearson correlation of two columns over the rows where both exist."""
    both = frame.drop_nulls([a, b])
    return float(both.select(pl.corr(a, b)).item())


def main(argv: list[str] | None = None) -> int:
    """Print the measures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    pl.Config.set_tbl_rows(20)

    info = load_station_information(args.data_dir)
    grid = build_grid(args.data_dir, args.start, args.end)
    if grid.height == 0 or info.height == 0:
        print("No data.")
        return 1

    near = nearest_neighbors(info, k=K)
    print("## 1. Distances")
    for rank in (1, K):
        median, p90 = (
            near.filter(pl.col("rank") == rank)
            .select(
                pl.col("distance_m").median().alias("median"),
                pl.col("distance_m").quantile(0.9).alias("p90"),
            )
            .row(0)
        )
        print(f"neighbour #{rank}: median {median:.0f} m, 90 % within {p90:.0f} m")
    within = near.filter(pl.col("rank") == 1).select((pl.col("distance_m") <= 300).mean()).item()
    print(f"stations with a neighbour within 300 m: {100 * within:.0f} %")

    far = random_far_pairs(info, K)
    network = grid.group_by("time").agg(pl.col("past").mean().alias("network_past"))
    table = (
        grid.join(neighbour_aggregates(grid, near, "near"), on=["station_id", "time"], how="left")
        .join(neighbour_aggregates(grid, far, "far"), on=["station_id", "time"], how="left")
        .join(network, on="time", how="left")
    )

    print("\n## 2. Co-movement of 15-minute changes (Pearson r)")
    print(f"station vs its {K} nearest:        {correlation(table, 'past', 'near_past'):+.3f}")
    print(f"station vs {K} random stations > 3 km: {correlation(table, 'past', 'far_past'):+.3f}")
    print(f"station vs the whole network:      {correlation(table, 'past', 'network_past'):+.3f}")

    print("\n## 3. What announces the next 60 minutes? (r with the future change)")
    for label, column in (
        ("own change over the last 15 min", "past"),
        (f"mean change of the {K} nearest", "near_past"),
        (f"mean change of {K} random far stations", "far_past"),
        ("mean change of the network", "network_past"),
    ):
        print(f"{label:<42} {correlation(table, column, 'future'):+.3f}")

    print("\n## 4. Unforeseen empty stations (not empty now, empty in 60 min)")
    candidates = table.filter(~pl.col("empty_now")).drop_nulls(["empty_later", "near_any_empty"])
    rates = (
        candidates.with_columns(low=pl.col("bikes") <= 2)
        .group_by("low", "near_any_empty")
        .agg(pl.col("empty_later").mean().alias("rate"), pl.len().alias("points"))
        .sort("low", "near_any_empty")
        .with_columns((100 * pl.col("rate")).round(2).alias("empty_in_60_min_%"))
        .drop("rate")
    )
    print("low = at most 2 bikes now; near_any_empty = one of the 5 nearest is empty now")
    print(rates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
