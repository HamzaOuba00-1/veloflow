"""Tests for the acceptance criteria 1 to 3."""

import polars as pl
import pytest

from veloflow.evaluation.acceptance import check_acceptance


def metrics(mae: dict[int, float], brier: float = 0.03, ece: float = 0.01) -> pl.DataFrame:
    """``all_metrics``-like rows: the same MAE for the three counts."""
    return pl.DataFrame(
        [
            {
                "horizon_min": horizon,
                "mechanical_mae": value,
                "ebike_mae": value,
                "docks_mae": value,
                "empty_brier": brier,
                "empty_ece": ece,
            }
            for horizon, value in mae.items()
        ]
    )


PERSISTENCE = metrics({15: 1.0, 30: 1.0, 60: 1.0}, brier=0.04)
PROFILE = metrics({15: 2.0, 30: 2.0, 60: 0.6}, brier=0.05)


def test_a_model_meeting_every_threshold_passes() -> None:
    # Values sit a little inside the thresholds: 1 - 0.8 / 1.0 is
    # 0.19999999999999996 in floating point, not 0.2.
    candidate = metrics({15: 0.89, 30: 0.79, 60: 0.5}, brier=0.039, ece=0.03)

    checks = check_acceptance(candidate, PERSISTENCE, PROFILE)

    assert len(checks) == 9 + 9 + 2
    assert all(check.passed for check in checks)
    skill = next(c for c in checks if c.criterion.startswith("3 Brier"))
    assert skill.value == pytest.approx(0.22)


def test_each_criterion_fails_on_its_own() -> None:
    candidate = metrics({15: 0.95, 30: 0.79, 60: 0.75}, brier=0.045, ece=0.031)

    failed = {
        (c.criterion, c.horizon_min)
        for c in check_acceptance(candidate, PERSISTENCE, PROFILE)
        if not c.passed
    }

    assert failed == {
        ("1 MAE gain over persistence", 15),  # 5 % < 10 %
        ("1 MAE gain over persistence", 60),  # 25 % < 30 %
        ("2 MAE below profile", 60),  # 0.75 > 0.6
        ("3 Brier skill vs historical frequency", 30),  # 0.1 < 0.2
        ("3 calibration error (ECE)", 30),  # 0.031 > 0.03
    }


def test_missing_values_fail() -> None:
    candidate = metrics({15: 0.5})

    checks = check_acceptance(candidate, PERSISTENCE.head(0), PROFILE.head(0))

    assert not any(check.passed for check in checks)
