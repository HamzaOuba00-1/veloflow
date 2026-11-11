"""Tests for the acceptance criteria 1 to 3."""

import polars as pl
import pytest

from veloflow.evaluation.acceptance import check_acceptance


def metrics(
    mae: dict[int, float], brier: float = 0.03, ece: float = 0.01, coverage: float = 0.8
) -> pl.DataFrame:
    """``all_metrics``-like rows: the same MAE and coverage for the three counts."""
    return pl.DataFrame(
        [
            {
                "horizon_min": horizon,
                **{f"{n}_mae": value for n in ("mechanical", "ebike", "docks")},
                **{f"{n}_coverage": coverage for n in ("mechanical", "ebike", "docks")},
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

    assert len(checks) == 9 + 9 + 2 + 9
    assert all(check.passed for check in checks)
    skill = next(c for c in checks if c.criterion.startswith("3 Brier"))
    assert skill.value == pytest.approx(0.22)


def test_each_criterion_fails_on_its_own() -> None:
    candidate = metrics({15: 0.95, 30: 0.79, 60: 0.75}, brier=0.045, ece=0.031, coverage=0.85)

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
        ("4 coverage of the 80 % interval", 15),  # 85 % > 83 %
        ("4 coverage of the 80 % interval", 30),
        ("4 coverage of the 80 % interval", 60),
    }


def test_missing_values_fail() -> None:
    # No reference to compare with, and no interval at all.
    candidate = metrics({15: 0.5}).select(pl.exclude("^.*_coverage$"))

    checks = check_acceptance(candidate, PERSISTENCE.head(0), PROFILE.head(0))

    assert not any(check.passed for check in checks)


def test_subgroups_degrading_more_than_15_percent_fail() -> None:
    subgroups = pl.DataFrame(
        {
            "group": ["all", "suburbs", "rain", "all", "suburbs"],
            "horizon_min": [15, 15, 15, 60, 60],
            "n": [100, 30, 0, 100, 30],
            "ratio": [0.90, 1.00, None, 0.80, 0.90],
        }
    )
    candidate = metrics({15: 0.89, 30: 0.79, 60: 0.5}, brier=0.039)

    checks = [
        c
        for c in check_acceptance(candidate, PERSISTENCE, PROFILE, subgroups)
        if c.criterion.startswith("5")
    ]

    # Suburbs: 1.00 / 0.90 = +11 % at 15 min, 0.90 / 0.80 = +12.5 % at 60 min;
    # rain has no example (null ratio): it fails rather than passing silently.
    assert [(c.horizon_min, c.quantity, c.passed) for c in checks] == [
        (15, "suburbs", True),
        (15, "rain", False),
        (60, "suburbs", True),
    ]
    assert checks[0].value == pytest.approx(1.0 / 0.9 - 1)
