"""Backtest report: run the baselines, compare them, check the criteria.

``veloflow-backtest`` evaluates every model on the same test days and writes
a Markdown report and the metrics (Parquet) to ``data/reports/backtest/``.
Headline numbers leave out suspected frozen stations (rule R4b).
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from veloflow.datasets import load_station_status, silver_days
from veloflow.evaluation.acceptance import Check, check_acceptance
from veloflow.evaluation.backtest import BacktestResult, run_backtest
from veloflow.evaluation.examples import EXCLUSION_REASONS, TARGETS
from veloflow.evaluation.metrics import all_metrics, regression_metrics, with_groups
from veloflow.models.base import Forecaster
from veloflow.models.baselines import Persistence, ProfileDelta, StationProfile

logger = logging.getLogger(__name__)

REPORT_DIR = Path("reports") / "backtest"
HOURLY_HORIZON_MIN = 60


def baselines() -> list[Forecaster]:
    """The models of the report; the first two are the references."""
    return [Persistence(), StationProfile(), ProfileDelta()]


def headline(result: BacktestResult) -> pl.DataFrame:
    """Scored examples without suspected frozen stations."""
    return result.frame.filter(~pl.col("suspected_frozen"))


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *["| " + " | ".join(row) + " |" for row in rows],
    ]


def render(
    results: list[BacktestResult], checks: dict[str, list[Check]], generated: datetime
) -> str:
    """The Markdown report of a backtest run."""
    first = results[0]
    days = [fold.day.isoformat() for fold in first.folds]
    lines = [
        "# Backtest report",
        "",
        f"Generated {generated:%Y-%m-%d %H:%M} UTC. Test days (Paris): {', '.join(days)}.",
        "Headline numbers leave out suspected frozen stations (rule R4b).",
        "",
        "## Examples per test day",
        "",
    ]
    lines += _table(
        ["Day", "Candidates", "Kept", *EXCLUSION_REASONS],
        [
            [
                fold.day.isoformat(),
                f"{fold.candidates:,}",
                f"{fold.kept:,}",
                *[f"{fold.excluded[name]:,}" for name in EXCLUSION_REASONS],
            ]
            for fold in first.folds
        ],
    )
    metrics = {result.model: all_metrics(headline(result)) for result in results}
    persistence = metrics[first.model]
    lines += ["", "## Counts: mean absolute error (skill against persistence)", ""]
    rows = []
    for model, frame in metrics.items():
        for row, base in zip(
            frame.iter_rows(named=True), persistence.iter_rows(named=True), strict=True
        ):
            cells = []
            for name in TARGETS:
                mae, floor = row[f"{name}_mae"], base[f"{name}_mae"]
                # Skill is undefined when persistence makes no error at all.
                skill = 1 - mae / floor if floor else None
                cells.append(f"{_fmt(mae)} ({_fmt(skill, 2)})")
            rows.append([model, str(row["horizon_min"]), f"{row['n']:,}", *cells])
    lines += _table(["Model", "Horizon", "Examples", *TARGETS], rows)

    lines += ["", "## Events: Brier score / calibration error / AUC-PR", ""]
    rows = []
    for model, frame in metrics.items():
        for row in frame.iter_rows(named=True):
            cells = [
                " / ".join(
                    (
                        _fmt(row[f"{event}_brier"], 4),
                        _fmt(row[f"{event}_ece"], 4),
                        _fmt(row[f"{event}_auc_pr"], 2),
                    )
                )
                for event in ("empty", "full")
            ]
            rows.append([model, str(row["horizon_min"]), _fmt(row["empty_rate"], 3), *cells])
    lines += _table(["Model", "Horizon", "Empty rate", "empty", "full"], rows)

    lines += [
        "",
        f"## Mechanical bikes, MAE by Paris hour at {HOURLY_HORIZON_MIN} minutes",
        "",
    ]
    hourly = {
        result.model: regression_metrics(
            with_groups(headline(result).filter(pl.col("horizon_min") == HOURLY_HORIZON_MIN)),
            ["hour"],
        )
        for result in results
    }
    hours = sorted(set().union(*[frame["hour"].to_list() for frame in hourly.values()]))
    rows = []
    for hour in hours:
        cells = []
        for frame in hourly.values():
            match = frame.filter(pl.col("hour") == hour)
            cells.append(_fmt(match["mechanical_mae"].item() if match.height else None, 2))
        rows.append([f"{hour:02d}h", *cells])
    lines += _table(["Hour", *hourly], rows)

    lines += ["", "## Acceptance criteria 1-3 (design, section 4.5)", ""]
    for model, model_checks in checks.items():
        passed = sum(check.passed for check in model_checks)
        lines += [f"### {model}: {passed} of {len(model_checks)} checks passed", ""]
        lines += _table(
            ["Criterion", "Horizon", "Quantity", "Value", "Threshold", "Result"],
            [
                [
                    check.criterion,
                    str(check.horizon_min),
                    check.quantity,
                    _fmt(check.value),
                    _fmt(check.threshold),
                    "pass" if check.passed else "FAIL",
                ]
                for check in model_checks
            ],
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(
    data_dir: Path, start: date, end: date, models: list[Forecaster]
) -> tuple[str, pl.DataFrame]:
    """Backtest ``models`` on the silver days ``start`` to ``end`` (UTC).

    Returns:
        The Markdown report, and every metric by model and horizon.
    """
    status = load_station_status(data_dir, start, end).frame
    results = [run_backtest(model, status) for model in models]
    if not results[0].folds:
        raise ValueError(f"no complete test day between {start} and {end}")
    metrics = {result.model: all_metrics(headline(result)) for result in results}
    persistence, profile = metrics[results[0].model], metrics[results[1].model]
    checks = {
        result.model: check_acceptance(metrics[result.model], persistence, profile)
        for result in results[2:]
    }
    table = pl.concat(
        [frame.with_columns(model=pl.lit(model)) for model, frame in metrics.items()],
        how="diagonal",
    )
    return render(results, checks, datetime.now(UTC)), table


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="Backtest the baselines and write a report.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--start", type=date.fromisoformat, help="first silver day (UTC)")
    parser.add_argument("--end", type=date.fromisoformat, help="last silver day (UTC)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the backtest on every silver day by default and write the report."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args(argv)
    days = silver_days(args.data_dir)
    if not days:
        logger.error("no silver station_status in %s", args.data_dir)
        return 1
    start, end = args.start or days[0], args.end or days[-1]
    try:
        report, metrics = run(args.data_dir, start, end, baselines())
    except ValueError as error:
        logger.error("%s", error)
        return 1
    folder = args.data_dir / REPORT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = f"backtest_{start.isoformat()}_{end.isoformat()}"
    (folder / f"{name}.md").write_text(report, encoding="utf-8")
    metrics.write_parquet(folder / f"{name}.parquet")
    logger.info("report written to %s", folder / f"{name}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
