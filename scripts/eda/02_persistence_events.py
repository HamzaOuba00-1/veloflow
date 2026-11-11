"""Exploration 3.2: how hard is persistence to beat, and how do stations run out?

Persistence predicts that a station at ``t + h`` will look exactly as it does at
``t``. It is the baseline every model must beat. This script measures, on a
5-minute grid:

1. the error of persistence at 15, 30 and 60 minutes, per target and per hour;
2. how well persistence predicts empty and full stations, and which share of
   those events it cannot see coming (stations that were not empty at ``t``);
3. empty and full episodes: how many, how long, concentrated on which stations.

Exploratory script: loading, cleaning and resampling live in
``veloflow.datasets``. Figures go to ``data/eda/``.

Usage:
    uv run python scripts/eda/02_persistence_events.py --data-dir data --start 2026-11-08
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

from veloflow.datasets import load_station_information, load_station_status, resample_status

STEP_MINUTES = 5
HORIZONS_MINUTES = (15, 30, 60)
TARGETS = ("mechanical", "ebike", "docks")


def grid_with_futures(data_dir: Path, start: date | None, end: date | None) -> pl.DataFrame:
    """5-minute grid of operational stations, with each target ``h`` minutes later."""
    data = load_station_status(data_dir, start, end)
    grid = resample_status(data.frame, every=f"{STEP_MINUTES}m", tolerance=f"{STEP_MINUTES}m")
    grid = grid.with_columns(
        bikes=pl.col("mechanical").cast(pl.Int32) + pl.col("ebike"),
        local_hour=pl.col("time").dt.convert_time_zone("Europe/Paris").dt.hour(),
    )
    futures = []
    for horizon in HORIZONS_MINUTES:
        steps = horizon // STEP_MINUTES
        for column in (*TARGETS, "bikes", "operational"):
            futures.append(
                pl.col(column).shift(-steps).over("station_id").alias(f"{column}_{horizon}")
            )
    return grid.with_columns(futures).filter(pl.col("operational"))


def persistence_errors(grid: pl.DataFrame) -> pl.DataFrame:
    """MAE and share of unchanged values of persistence, per target and horizon."""
    rows = []
    for horizon in HORIZONS_MINUTES:
        valid = grid.filter(pl.col(f"operational_{horizon}").fill_null(value=False))
        for target in (*TARGETS, "bikes"):
            error = (pl.col(f"{target}_{horizon}").cast(pl.Int32) - pl.col(target)).abs()
            stats = valid.select(
                error.mean().alias("mae"),
                (error == 0).mean().alias("unchanged"),
                (error <= 2).mean().alias("within_2"),
                pl.len().alias("pairs"),
            ).row(0, named=True)
            rows.append({"horizon_min": horizon, "target": target, **stats})
    return (
        pl.DataFrame(rows)
        .with_columns(
            pl.col("mae").round(2),
            (100 * pl.col("unchanged")).round(1).alias("unchanged_%"),
            (100 * pl.col("within_2")).round(1).alias("within_2_%"),
        )
        .drop("unchanged", "within_2")
    )


def event_scores(grid: pl.DataFrame) -> pl.DataFrame:
    """Persistence as a classifier of events at ``t + h``."""
    # Event name -> column that is zero when the event happens.
    events = {"empty": "bikes", "no_ebike": "ebike", "no_mechanical": "mechanical", "full": "docks"}
    rows = []
    for horizon in HORIZONS_MINUTES:
        valid = grid.filter(pl.col(f"operational_{horizon}").fill_null(value=False))
        for name, column in events.items():
            now = pl.col(column) == 0
            later = pl.col(f"{column}_{horizon}") == 0
            stats = valid.select(
                later.mean().alias("base_rate"),
                (now & later).sum().alias("both"),
                now.sum().alias("predicted"),
                later.sum().alias("actual"),
            ).row(0, named=True)
            rows.append(
                {
                    "event": name,
                    "horizon_min": horizon,
                    "base_rate_%": round(100 * stats["base_rate"], 1),
                    "precision_%": round(100 * stats["both"] / max(stats["predicted"], 1), 1),
                    "recall_%": round(100 * stats["both"] / max(stats["actual"], 1), 1),
                    "unforeseen_%": round(
                        100 * (stats["actual"] - stats["both"]) / max(stats["actual"], 1), 1
                    ),
                }
            )
    return pl.DataFrame(rows).sort("event", "horizon_min")


def episodes(grid: pl.DataFrame, condition: pl.Expr, name: str) -> pl.DataFrame:
    """Runs of consecutive grid points where ``condition`` holds, per station."""
    runs = (
        grid.sort("station_id", "time")
        .with_columns(flag=condition)
        # A new run starts whenever the flag changes within a station.
        .with_columns(
            run=(pl.col("flag") != pl.col("flag").shift(1).over("station_id"))
            .fill_null(value=True)
            .cum_sum()
        )
        .filter(pl.col("flag"))
        .group_by("station_id", "run")
        .agg((pl.len() * STEP_MINUTES).alias("minutes"))
    )
    return runs.with_columns(pl.lit(name).alias("event"))


def plot_mae_by_hour(grid: pl.DataFrame, path: Path) -> None:
    """Persistence error on available bikes by hour of the day (Paris time)."""
    figure, axis = plt.subplots(figsize=(9, 4))
    for horizon in HORIZONS_MINUTES:
        by_hour = (
            grid.filter(pl.col(f"operational_{horizon}").fill_null(value=False))
            .group_by("local_hour")
            .agg((pl.col(f"bikes_{horizon}") - pl.col("bikes")).abs().mean().alias("mae"))
            .sort("local_hour")
        )
        axis.plot(by_hour["local_hour"], by_hour["mae"], marker="o", label=f"{horizon} min")
    axis.set_xlabel("hour of the day (Paris)")
    axis.set_ylabel("mean absolute error (bikes)")
    axis.set_title("Persistence error on available bikes, by hour")
    axis.set_xticks(range(0, 24, 2))
    axis.grid(alpha=0.3)
    axis.legend(title="horizon")
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    plt.close(figure)


def main(argv: list[str] | None = None) -> int:
    """Print the measures and write the figures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    out = args.data_dir / "eda"
    out.mkdir(parents=True, exist_ok=True)
    pl.Config.set_tbl_rows(40)
    pl.Config.set_tbl_cols(12)

    grid = grid_with_futures(args.data_dir, args.start, args.end)
    if grid.height == 0:
        print("No data.")
        return 1
    print(f"grid points (operational station x 5 min): {grid.height:,}")

    print("\n## 1. Persistence error")
    print(persistence_errors(grid))

    print("\n## 2. Persistence as an event predictor")
    print(event_scores(grid))

    print("\n## 3. Episodes (5-minute resolution)")
    runs = pl.concat(
        [
            episodes(grid, pl.col("bikes") == 0, "empty"),
            episodes(grid, pl.col("docks") == 0, "full"),
        ]
    )
    print(
        runs.group_by("event")
        .agg(
            pl.len().alias("episodes"),
            pl.col("station_id").n_unique().alias("stations"),
            pl.col("minutes").median().alias("median_min"),
            pl.col("minutes").quantile(0.9).alias("p90_min"),
            pl.col("minutes").max().alias("max_min"),
        )
        .sort("event")
    )
    info = load_station_information(args.data_dir).select("station_id", "name", "capacity")
    for event in ("empty", "full"):
        per_station = (
            runs.filter(pl.col("event") == event)
            .group_by("station_id")
            .agg(pl.col("minutes").sum().alias("minutes"))
            .sort("minutes", descending=True)
        )
        top = float(per_station.head(max(1, per_station.height // 10))["minutes"].sum())
        total = max(float(per_station["minutes"].sum()), 1.0)
        print(
            f"{event}: the 10 % most affected stations hold "
            f"{100 * top / total:.0f} % of the {event} time"
        )
        print(per_station.head(5).join(info, on="station_id", how="left"))

    plot_mae_by_hour(grid, out / "02_persistence_by_hour.png")
    print(f"\nfigure: {out / '02_persistence_by_hour.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
