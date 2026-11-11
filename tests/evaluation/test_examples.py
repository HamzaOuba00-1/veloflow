"""Tests for the evaluation examples."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import polars as pl

from veloflow.evaluation.examples import EXCLUSION_REASONS, build_examples

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def grid(
    states: Sequence[tuple[int, int, int] | None],
    *,
    station: int = 1,
    operational: list[bool] | None = None,
) -> pl.DataFrame:
    """A 5-minute grid for one station; ``None`` is an unknown state."""
    flags = operational or [True] * len(states)
    return pl.DataFrame(
        {
            "station_id": [station] * len(states),
            "time": [T0 + k * STEP for k in range(len(states))],
            "fetched_at": [None if s is None else T0 + k * STEP for k, s in enumerate(states)],
            "mechanical": [None if s is None else s[0] for s in states],
            "ebike": [None if s is None else s[1] for s in states],
            "docks": [None if s is None else s[2] for s in states],
            "operational": [None if s is None else f for s, f in zip(states, flags, strict=True)],
        },
        schema={
            "station_id": pl.Int64,
            "time": pl.Datetime("us", "UTC"),
            "fetched_at": pl.Datetime("us", "UTC"),
            "mechanical": pl.Int16,
            "ebike": pl.Int16,
            "docks": pl.Int16,
            "operational": pl.Boolean,
        },
    )


def test_targets_are_read_horizon_minutes_later() -> None:
    states = [(k, 1, 10 - k) for k in range(5)]  # 12:00 .. 12:20
    states[4] = (0, 0, 9)

    examples = build_examples(grid(states), horizons=(5, 15))

    # Horizon 5: four origins have a target on the grid; horizon 15: two.
    assert examples.candidates == 6
    assert all(count == 0 for count in examples.excluded.values())
    frame = examples.frame.filter(pl.col("horizon_min") == 15)
    assert frame.select("origin", "mechanical", "y_mechanical", "y_docks").rows() == [
        (T0, 0, 3, 7),
        (T0 + STEP, 1, 0, 9),
    ]
    assert frame["target_at"].to_list() == [T0 + 3 * STEP, T0 + 4 * STEP]
    assert frame["y_empty"].to_list() == [False, True]


def test_empty_and_full_events() -> None:
    examples = build_examples(grid([(1, 0, 0), (0, 0, 5), (0, 2, 0)]), horizons=(5,))

    assert examples.frame.select("y_empty", "y_full").rows() == [(True, False), (False, True)]


def test_each_candidate_is_excluded_for_its_first_reason() -> None:
    state = (2, 1, 5)
    states: list[tuple[int, int, int] | None] = [state] * 12
    states[1] = None  # origin 12:05 unknown; also the target of 12:00
    operational = [True] * 12
    operational[3] = False  # 12:15 closed
    frozen_slots = pl.DataFrame(
        {"slot_start": [T0 + 6 * STEP]}, schema={"slot_start": pl.Datetime("us", "UTC")}
    )  # 12:30-12:44

    examples = build_examples(
        grid(states, operational=operational), horizons=(5,), frozen_slots=frozen_slots
    )

    assert examples.candidates == 11
    assert examples.excluded == {
        "origin_unknown": 1,  # 12:05
        "origin_not_operational": 1,  # 12:15
        "origin_frozen_feed": 3,  # 12:30, 12:35, 12:40
        "target_unknown": 1,  # 12:00 -> 12:05
        "target_not_operational": 1,  # 12:10 -> 12:15
        "target_frozen_feed": 1,  # 12:25 -> 12:30
    }
    assert examples.frame["origin"].to_list() == [T0 + 4 * STEP, T0 + 9 * STEP, T0 + 10 * STEP]
    assert list(examples.excluded) == list(EXCLUSION_REASONS)


def test_suspected_frozen_stations_are_flagged_not_removed() -> None:
    frozen = pl.DataFrame(
        {
            "station_id": [1, 1],
            "frozen_from": [T0 + 2 * STEP, T0 + 6 * STEP],
            "frozen_until": [T0 + 3 * STEP, T0 + 7 * STEP],
            "daytime_hours": [12.5, 13.0],
        },
        schema={
            "station_id": pl.Int64,
            "frozen_from": pl.Datetime("us", "UTC"),
            "frozen_until": pl.Datetime("us", "UTC"),
            "daytime_hours": pl.Float64,
        },
    )
    two_stations = pl.concat([grid([(3, 0, 4)] * 8), grid([(3, 0, 4)] * 8, station=2)])

    examples = build_examples(two_stations, horizons=(5,), frozen_stations=frozen)

    flagged = examples.frame.filter(pl.col("suspected_frozen"))
    # Station 1: 12:10 and 12:30 are frozen (the end is excluded), so the
    # origins 12:05, 12:10, 12:25 and 12:30 touch a frozen period.
    assert flagged.select("station_id", "origin").rows() == [
        (1, T0 + k * STEP) for k in (1, 2, 5, 6)
    ]
    assert examples.frame.height == 14


def test_empty_grid() -> None:
    examples = build_examples(grid([]))

    assert examples.frame.height == 0
    assert examples.candidates == 0
