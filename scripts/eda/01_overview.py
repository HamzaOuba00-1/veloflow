"""Exploration 3.1: what data do we have, and how clean is it?

Exploratory script, not production code: the loading and cleaning logic lives
in ``veloflow.datasets``; this script only measures and plots. It prints a
summary and writes its figures to ``data/eda/`` (ignored by Git). Selected
figures and conclusions go to ``docs/data/eda.md``.

Usage (5 November is a 17-minute exploration sample, hence the start date):
    uv run python scripts/eda/01_overview.py --data-dir data --start 2026-11-08
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib as mpl

mpl.use("Agg")  # files only, no window
import matplotlib.pyplot as plt
import polars as pl

from veloflow.datasets import load_station_information, load_station_status

PARIS = ZoneInfo("Europe/Paris")


def coverage_table(data_dir: Path) -> pl.DataFrame:
    """Daily coverage reports (rule R6) as a table."""
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((data_dir / "quality" / "coverage").glob("coverage_*.json"))
    ]
    return pl.DataFrame(
        [
            {
                "day": row["day"],
                "expected": row["expected_versions"],
                "usable_%": round(100 * row["usable_share"], 2),
                "source_failures_%": round(100 * row["source_failure_share"], 2),
                "silent_%": round(100 * row["silent_missed_share"], 2),
                "gaps": len(row["gaps"]),
            }
            for row in rows
        ]
    )


def share(frame: pl.DataFrame, condition: pl.Expr) -> float:
    """Share of the rows of ``frame`` meeting ``condition``."""
    return float(frame.select(condition.mean()).item())


def network_totals(frame: pl.DataFrame) -> pl.DataFrame:
    """Bikes and free docks summed over operational stations, per version."""
    return (
        frame.filter(pl.col("operational"))
        .group_by("fetched_at")
        .agg(
            pl.col("mechanical").cast(pl.Int32).sum(),
            pl.col("ebike").cast(pl.Int32).sum(),
            pl.col("docks").cast(pl.Int32).sum(),
            pl.len().alias("stations"),
        )
        .sort("fetched_at")
        .with_columns(local_time=pl.col("fetched_at").dt.convert_time_zone("Europe/Paris"))
    )


def plot_network(totals: pl.DataFrame, path: Path) -> None:
    """Bikes parked in operational stations over time (Paris time)."""
    figure, axis = plt.subplots(figsize=(11, 4))
    axis.plot(totals["local_time"], totals["mechanical"], label="mechanical bikes", lw=1)
    axis.plot(totals["local_time"], totals["ebike"], label="e-bikes", lw=1)
    axis.set_ylabel("bikes parked in stations")
    axis.set_title("Vélib' network: bikes parked in operational stations (Paris time)")
    axis.grid(alpha=0.3)
    axis.legend()
    figure.autofmt_xdate()
    figure.tight_layout()
    figure.savefig(path, dpi=110)
    plt.close(figure)


def main(argv: list[str] | None = None) -> int:
    """Print the overview and write the figures."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=date.fromisoformat, help="first UTC day (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="last UTC day (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    out = args.data_dir / "eda"
    out.mkdir(parents=True, exist_ok=True)
    pl.Config.set_tbl_rows(40)
    pl.Config.set_tbl_cols(12)

    data = load_station_status(args.data_dir, args.start, args.end)
    frame = data.frame
    if frame.height == 0:
        print("No silver station_status data found.")
        return 1
    info = load_station_information(args.data_dir)

    print("## Volume")
    print(f"days: {[str(day) for day in data.days]}")
    print(
        f"versions kept: {data.versions_kept}, stale removed (R1): {data.versions_stale}, "
        f"ties replaced: {data.ties_replaced}"
    )
    print(f"rows: {frame.height:,}, stations seen: {frame['station_id'].n_unique()}")
    print(f"stations in the reference: {info.height}")
    print(
        frame.with_columns(day=pl.col("fetched_at").dt.date())
        .group_by("day")
        .agg(pl.col("fetched_at").n_unique().alias("versions"), pl.col("station_id").n_unique())
        .sort("day")
    )

    print("\n## Coverage (rule R6)")
    print(coverage_table(args.data_dir))

    print("\n## Station states")
    operational = frame.filter(pl.col("operational"))
    bikes = pl.col("mechanical").cast(pl.Int32) + pl.col("ebike")
    print(f"operational rows: {100 * operational.height / frame.height:.1f} %")
    for label, condition in (
        ("empty (no bike)", bikes == 0),
        ("no mechanical bike", pl.col("mechanical") == 0),
        ("no e-bike", pl.col("ebike") == 0),
        ("full (no free dock)", pl.col("docks") == 0),
    ):
        print(f"{label:<20} {100 * share(operational, condition):.1f} %")

    totals = network_totals(frame)
    print("\n## Network totals (operational stations)")
    print(
        totals.select("mechanical", "ebike", "docks", "stations")
        .describe()
        .filter(pl.col("statistic").is_in(["min", "mean", "max"]))
    )
    plot_network(totals, out / "01_network_bikes.png")
    print(f"\nfigure: {out / '01_network_bikes.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
