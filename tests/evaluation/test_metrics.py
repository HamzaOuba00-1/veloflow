"""Tests for the forecast metrics, against values computed by hand."""

import math
from datetime import UTC, datetime

import polars as pl
import pytest

from veloflow.evaluation.metrics import (
    all_metrics,
    event_metrics,
    interval_metrics,
    regression_metrics,
    with_groups,
)


def counts_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "horizon_min": [15, 15, 60],
            "mechanical": [3, 0, 5],
            "ebike": [1, 1, 1],
            "docks": [6, 9, 4],
            "y_mechanical": [4, 0, 2],
            "y_ebike": [1, 1, 1],
            "y_docks": [5, 9, 7],
            "pred_mechanical": [3.5, 1.0, 2.0],
            "pred_ebike": [1.0, 1.0, 1.0],
            "pred_docks": [5.0, 9.0, 4.0],
        }
    )


def test_regression_metrics_carry_persistence() -> None:
    result = regression_metrics(counts_frame())

    row = result.row(0, named=True)
    assert row["n"] == 2
    assert row["mechanical_mae"] == pytest.approx(0.75)  # |3.5-4|, |1-0|
    assert row["mechanical_rmse"] == pytest.approx(math.sqrt((0.25 + 1.0) / 2))
    assert row["mechanical_mae_persistence"] == pytest.approx(0.5)  # |3-4|, |0-0|
    assert row["docks_mae"] == 0.0
    assert result.row(1, named=True)["docks_mae"] == pytest.approx(3.0)


def events_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "horizon_min": [30, 30, 30, 30, 60],
            "y_empty": [True, False, True, False, False],
            "p_empty": [0.9, 0.8, 0.8, 0.1, 0.3],
            "y_full": [False, False, False, False, False],
            "p_full": [0.0, 0.0, 0.0, 1.0, 0.0],
        }
    )


def test_event_metrics_by_hand() -> None:
    row = event_metrics(events_frame()).row(0, named=True)

    assert row["n"] == 4
    assert row["empty_rate"] == 0.5
    assert row["empty_brier"] == pytest.approx((0.01 + 0.64 + 0.04 + 0.01) / 4)
    expected_log_loss = -(math.log(0.9) + math.log(0.2) + math.log(0.8) + math.log(0.9)) / 4
    assert row["empty_log_loss"] == pytest.approx(expected_log_loss)
    # Bins 9 (0.9 vs 1), 8 (0.8 vs 0.5, two examples), 1 (0.1 vs 0).
    assert row["empty_ece"] == pytest.approx(0.25 * 0.1 + 0.5 * 0.3 + 0.25 * 0.1)
    # Thresholds 0.9 (precision 1), then 0.8 tied (precision 2/3).
    assert row["empty_auc_pr"] == pytest.approx((1.0 + 2 / 3) / 2)


def test_rare_event_without_positive_and_confident_miss() -> None:
    result = event_metrics(events_frame())

    assert result["full_auc_pr"].to_list() == [None, None]
    # One confident miss (p = 1 for a non-event) costs -ln(1e-6), not infinity.
    assert result["full_log_loss"][0] == pytest.approx(-math.log(1e-6) / 4)
    assert result["full_brier"][0] == pytest.approx(0.25)


def test_groups_use_paris_time() -> None:
    frame = pl.DataFrame(
        {
            "origin": [
                datetime(2026, 11, 9, 23, 30, tzinfo=UTC),  # Tuesday 00:30 in Paris
                datetime(2026, 11, 14, 7, 0, tzinfo=UTC),  # Saturday 08:00
            ]
        }
    )

    assert with_groups(frame).select("hour", "day_type").rows() == [
        (0, "weekday"),
        (8, "weekend"),
    ]


def test_interval_coverage_includes_bounds() -> None:
    frame = counts_frame().with_columns(
        lo_mechanical=pl.Series([4.0, 1.0, 0.0]),  # 4 in [4, 5]; 0 not in [1, 2]
        hi_mechanical=pl.Series([5.0, 2.0, 2.0]),
        lo_ebike=pl.Series([0.0, 0.0, 0.0]),
        hi_ebike=pl.Series([2.0, 2.0, 2.0]),
        lo_docks=pl.Series([5.0, 9.0, 3.0]),
        hi_docks=pl.Series([5.0, 9.0, 5.0]),
    )

    result = interval_metrics(frame)

    row = result.row(0, named=True)
    assert row["mechanical_coverage"] == 0.5
    assert row["mechanical_width"] == 1.0
    assert row["docks_coverage"] == 1.0
    assert (
        "mechanical_coverage"
        in all_metrics(
            frame.with_columns(
                y_empty=pl.lit(False), p_empty=pl.lit(0.0), y_full=pl.lit(False), p_full=pl.lit(0.0)
            )
        ).columns
    )
