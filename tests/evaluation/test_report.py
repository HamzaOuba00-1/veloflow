"""Tests for the backtest report and its command."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from veloflow.evaluation.report import main, run
from veloflow.models.baselines import Persistence, ProfileDelta, StationProfile


def write_silver(status: pl.DataFrame, data_dir: Path) -> None:
    """Write ``status`` as silver files, one per UTC day."""
    frame = status.drop("operational").with_columns(day=pl.col("fetched_at").dt.date())
    for (day,), part in frame.group_by("day"):
        folder = data_dir / "silver" / "station_status" / f"date={day}"
        folder.mkdir(parents=True)
        part.drop("day").write_parquet(folder / "station_status.parquet")


def test_report_covers_every_section(synthetic_status: pl.DataFrame, tmp_path: Path) -> None:
    write_silver(synthetic_status, tmp_path)

    report, metrics = run(
        tmp_path,
        date(2026, 11, 8),
        date(2026, 11, 10),
        [Persistence(), StationProfile(), ProfileDelta()],
    )

    assert "Test days (Paris): 2026-11-09, 2026-11-10." in report
    for section in ("Examples per test day", "mean absolute error", "Brier", "by Paris hour"):
        assert section in report
    assert "### persistence_profile_delta: " in report
    # 2 days x 288 origins x 2 stations at each horizon.
    assert "| persistence | 15 | 1,152 | 1.200 (0.00) |" in report
    # On station 2 e-bikes alternate 0 and 1 and station 1 has none, so
    # persistence is exact at 30 minutes (6 steps): its skill is undefined.
    assert "| persistence | 30 | 1,152 | 0.799 (0.00) | 0.000 (n/a) |" in report
    assert metrics["model"].unique().sort().to_list() == [
        "persistence",
        "persistence_profile_delta",
        "profile",
    ]


def test_command_writes_report_and_metrics(synthetic_status: pl.DataFrame, tmp_path: Path) -> None:
    write_silver(synthetic_status, tmp_path)

    assert main(["--data-dir", str(tmp_path)]) == 0

    folder = tmp_path / "reports" / "backtest"
    assert (
        (folder / "backtest_2026-11-08_2026-11-10.md")
        .read_text(encoding="utf-8")
        .startswith("# Backtest report")
    )
    assert pl.read_parquet(folder / "backtest_2026-11-08_2026-11-10.parquet").height == 9


def test_command_fails_without_data_or_test_day(
    synthetic_status: pl.DataFrame, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert main(["--data-dir", str(tmp_path)]) == 1

    write_silver(synthetic_status, tmp_path)
    assert main(["--data-dir", str(tmp_path), "--end", "2026-11-08"]) == 1
    assert "no complete test day" in caplog.text
