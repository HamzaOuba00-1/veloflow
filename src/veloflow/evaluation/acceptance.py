"""Acceptance criteria 1 to 3 of the design (section 4.5), checked automatically.

1. MAE at least 10 %, 20 % and 30 % below persistence at 15, 30 and 60
   minutes, for each count.
2. MAE below the historical profile of the station at every horizon.
3. For P(empty) at 30 minutes: Brier skill score of at least 0.2 against the
   historical frequency (the profile's probabilities), and a calibration error
   (ECE) of at most 0.03.
4. Coverage of the 80 % interval between 77 % and 83 %, for each count.
5. No degradation of more than 15 % on a subgroup (``subgroups``).

Criteria 6 and 7 (new stations, decisions in simulation) need a cold-start
protocol and a simulator that do not exist yet.
"""

from dataclasses import dataclass

import polars as pl

from veloflow.evaluation.examples import TARGETS

MAE_GAIN_OVER_PERSISTENCE: dict[int, float] = {15: 0.10, 30: 0.20, 60: 0.30}
EVENT_HORIZON_MIN = 30
MIN_BRIER_SKILL = 0.2
MAX_ECE = 0.03
COVERAGE_RANGE = (0.77, 0.83)
MAX_SUBGROUP_DEGRADATION = 0.15


@dataclass(frozen=True, slots=True)
class Check:
    """One criterion on one horizon and one quantity."""

    criterion: str
    horizon_min: int
    quantity: str
    value: float | None
    threshold: float
    passed: bool


def _by_horizon(metrics: pl.DataFrame) -> dict[int, dict[str, float]]:
    return {int(row["horizon_min"]): row for row in metrics.iter_rows(named=True)}


def check_acceptance(
    candidate: pl.DataFrame,
    persistence: pl.DataFrame,
    profile: pl.DataFrame,
    subgroups: pl.DataFrame | None = None,
) -> list[Check]:
    """Compare a candidate with the two reference baselines.

    Args:
        candidate: ``metrics.all_metrics`` of the model, by ``horizon_min``.
        persistence: the same for persistence, on the same examples.
        profile: the same for the station profile, on the same examples.
        subgroups: ``subgroups.subgroup_ratios`` of the candidate; subgroups
            without any example are not checked.

    Returns:
        One check per criterion, horizon and quantity. A missing value fails.
    """
    model, base, reference = _by_horizon(candidate), _by_horizon(persistence), _by_horizon(profile)
    checks: list[Check] = []
    for horizon, required in MAE_GAIN_OVER_PERSISTENCE.items():
        for name in TARGETS:
            mae = model.get(horizon, {}).get(f"{name}_mae")
            floor = base.get(horizon, {}).get(f"{name}_mae")
            gain = None if mae is None or not floor else 1.0 - mae / floor
            checks.append(
                Check(
                    "1 MAE gain over persistence",
                    horizon,
                    name,
                    gain,
                    required,
                    gain is not None and gain >= required,
                )
            )
    for horizon in sorted(model):
        for name in TARGETS:
            mae = model[horizon].get(f"{name}_mae")
            ceiling = reference.get(horizon, {}).get(f"{name}_mae")
            checks.append(
                Check(
                    "2 MAE below profile",
                    horizon,
                    name,
                    mae,
                    float("nan") if ceiling is None else ceiling,
                    mae is not None and ceiling is not None and mae < ceiling,
                )
            )
    row = model.get(EVENT_HORIZON_MIN, {})
    brier, reference_brier = (
        row.get("empty_brier"),
        reference.get(EVENT_HORIZON_MIN, {}).get("empty_brier"),
    )
    skill = None if brier is None or not reference_brier else 1.0 - brier / reference_brier
    checks.append(
        Check(
            "3 Brier skill vs historical frequency",
            EVENT_HORIZON_MIN,
            "empty",
            skill,
            MIN_BRIER_SKILL,
            skill is not None and skill >= MIN_BRIER_SKILL,
        )
    )
    ece = row.get("empty_ece")
    checks.append(
        Check(
            "3 calibration error (ECE)",
            EVENT_HORIZON_MIN,
            "empty",
            ece,
            MAX_ECE,
            ece is not None and ece <= MAX_ECE,
        )
    )
    low, high = COVERAGE_RANGE
    for horizon in sorted(model):
        for name in TARGETS:
            coverage = model[horizon].get(f"{name}_coverage")
            checks.append(
                Check(
                    "4 coverage of the 80 % interval",
                    horizon,
                    name,
                    coverage,
                    low,
                    coverage is not None and low <= coverage <= high,
                )
            )
    if subgroups is not None:
        overall = {
            int(r["horizon_min"]): r["ratio"]
            for r in subgroups.filter(pl.col("group") == "all").iter_rows(named=True)
        }
        for r in subgroups.filter(pl.col("group") != "all").iter_rows(named=True):
            ratio_all = overall.get(int(r["horizon_min"]))
            degradation = (
                None if r["ratio"] is None or not ratio_all else r["ratio"] / ratio_all - 1.0
            )
            checks.append(
                Check(
                    "5 degradation on a subgroup",
                    int(r["horizon_min"]),
                    r["group"],
                    degradation,
                    MAX_SUBGROUP_DEGRADATION,
                    degradation is not None and degradation <= MAX_SUBGROUP_DEGRADATION,
                )
            )
    return checks
