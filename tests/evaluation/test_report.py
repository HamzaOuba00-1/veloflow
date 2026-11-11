"""Tests for the backtest report and its command."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from veloflow.evaluation.report import all_models, main, run
from veloflow.features.build import make_context
from veloflow.models.baselines import Persistence, ProfileDelta, StationProfile
from veloflow.models.gbm import GradientBoosting


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

    assert main(["--data-dir", str(tmp_path), "--baselines-only"]) == 0

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
    assert main(["--data-dir", str(tmp_path), "--end", "2026-11-08", "--baselines-only"]) == 1
    assert "no complete test day" in caplog.text


def test_all_models_end_with_lightgbm(synthetic_status: pl.DataFrame, tmp_path: Path) -> None:
    write_silver(synthetic_status, tmp_path)

    models = all_models(tmp_path)

    assert [model.name for model in models] == [
        "persistence",
        "profile",
        "persistence_profile_delta",
        "lightgbm",
    ]


def test_report_of_a_model_with_intervals(synthetic_status: pl.DataFrame, tmp_path: Path) -> None:
    write_silver(synthetic_status, tmp_path)
    stations = pl.DataFrame(
        {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
        schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
    )
    small: dict[str, object] = {
        "n_estimators": 10,
        "num_leaves": 7,
        "min_child_samples": 5,
        "verbose": -1,
    }
    gbm = GradientBoosting(make_context(stations), params=small)

    report, metrics = run(
        tmp_path, date(2026, 11, 8), date(2026, 11, 10), [Persistence(), StationProfile(), gbm]
    )

    assert "## 80 % intervals: coverage / mean width" in report
    assert "## Subgroups" in report
    assert "4 coverage of the 80 % interval" in report
    assert "lightgbm" in metrics["model"].to_list()
