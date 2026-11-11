"""Tests for rule R4: frozen feed (R4a) and frozen stations (R4b)."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from veloflow.quality.freeze import change_counts, frozen_feed_slots, suspected_frozen_stations
from veloflow.quality.versions import with_operational
from veloflow.storage.compaction import STATUS_SCHEMA

MONDAY = datetime(2026, 11, 9, tzinfo=UTC)  # a working day


def status(rows: list[tuple[datetime, int, int, bool]]) -> pl.DataFrame:
    """Rows (fetched_at, station_id, mechanical bikes, installed)."""
    return with_operational(
        pl.DataFrame(
            [(t, t, s, bikes, 0, 10, installed, True, True, t) for t, s, bikes, installed in rows],
            schema=STATUS_SCHEMA,
            orient="row",
        )
    )


def test_change_counts_ignore_first_versions_and_closed_stations() -> None:
    t0, t1, t2 = (MONDAY + timedelta(minutes=m) for m in range(3))
    frame = status(
        [
            (t0, 1, 3, True),
            (t1, 1, 4, True),  # change
            (t2, 1, 4, True),
            (t0, 2, 5, True),
            (t1, 2, 5, True),
            (t2, 2, 6, True),  # change
            (t1, 3, 1, False),
            (t2, 3, 9, False),  # closed: ignored
        ]
    )

    assert change_counts(frame).rows() == [(t0, 0), (t1, 1), (t2, 1)]


def counts(per_minute: dict[datetime, int]) -> pl.DataFrame:
    return pl.DataFrame(
        {"fetched_at": list(per_minute), "changed": list(per_minute.values())},
        schema={"fetched_at": pl.Datetime("us", "UTC"), "changed": pl.Int64},
    )


def afternoon(day: int, changes: dict[int, int]) -> dict[datetime, int]:
    """One version a minute from 12:00 to 13:59 UTC; ``changes`` by minute offset."""
    start = MONDAY + timedelta(days=day, hours=12)
    return {start + timedelta(minutes=m): changes.get(m, 100) for m in range(120)}


def test_incident_is_flagged_with_its_catch_up() -> None:
    # Normal slot: 15 versions x 100 changes = 1,500. On Tuesday, 12:30-13:15
    # collapses; 12:45 is just above 30 % but sits between two frozen slots.
    incident = {m: 10 for m in range(30, 45)} | {m: 34 for m in range(45, 60)}
    incident |= {m: 10 for m in range(60, 75)} | {m: 300 for m in range(75, 90)}
    per_minute = afternoon(0, {}) | afternoon(1, incident) | afternoon(2, {})

    flagged = frozen_feed_slots(counts(per_minute))

    tuesday = MONDAY + timedelta(days=1, hours=12)
    assert flagged.select("slot_start", "reason").rows() == [
        (tuesday + timedelta(minutes=30), "frozen"),
        (tuesday + timedelta(minutes=45), "frozen"),
        (tuesday + timedelta(minutes=60), "frozen"),
        (tuesday + timedelta(minutes=75), "catch_up"),
    ]
    assert flagged["expected"][0] == 1500


def test_quiet_slots_are_never_incidents() -> None:
    # 2 changes a minute = 30 a slot, below ``min_expected``: nothing to compare.
    quiet = {m: 2 for m in range(120)}
    per_minute = (
        afternoon(0, quiet) | afternoon(1, {m: 0 for m in range(120)}) | afternoon(2, quiet)
    )

    assert frozen_feed_slots(counts(per_minute)).height == 0


def test_coverage_gap_is_not_an_incident() -> None:
    # Wednesday keeps a single version in 12:30-12:44: too few versions to judge.
    per_minute = afternoon(0, {}) | afternoon(1, {}) | afternoon(2, {})
    gap_start = MONDAY + timedelta(days=2, hours=12, minutes=30)
    per_minute = {
        t: c for t, c in per_minute.items() if not gap_start < t < gap_start + timedelta(minutes=15)
    }
    per_minute[gap_start] = 0

    assert frozen_feed_slots(counts(per_minute)).height == 0


def test_a_single_day_has_no_reference() -> None:
    assert frozen_feed_slots(counts(afternoon(0, {m: 0 for m in range(60)}))).height == 0


def test_station_suspected_after_twelve_daytime_hours() -> None:
    # Station 1 does not change from Monday 05:00 UTC (06:00 Paris) to Tuesday
    # 12:00 UTC. Daytime is 07:00-22:00 Paris = 06:00-21:00 UTC, so twelve
    # daytime hours are reached on Monday at 18:00 UTC.
    times = [MONDAY + timedelta(hours=5, minutes=30 * k) for k in range(63)]
    rows = [(t, 1, 0, True) for t in times] + [(times[-1] + timedelta(minutes=30), 1, 3, True)]
    # Station 2 changes every half hour: never suspected.
    rows += [(t, 2, k % 2, True) for k, t in enumerate(times)]

    frozen = suspected_frozen_stations(status(rows))

    assert frozen.rows() == [
        (
            1,
            MONDAY + timedelta(hours=18),
            times[-1] + timedelta(minutes=30),
            frozen["daytime_hours"][0],
        )
    ]
    assert frozen["daytime_hours"][0] >= 15.0


def test_a_run_still_open_ends_at_the_last_version() -> None:
    times = [MONDAY + timedelta(hours=6, minutes=30 * k) for k in range(30)]

    frozen = suspected_frozen_stations(status([(t, 7, 4, True) for t in times]))

    assert frozen["frozen_from"].to_list() == [MONDAY + timedelta(hours=18)]
    assert frozen["frozen_until"].to_list() == [times[-1]]


def test_closed_station_is_not_suspected() -> None:
    times = [MONDAY + timedelta(hours=6, minutes=30 * k) for k in range(40)]

    assert suspected_frozen_stations(status([(t, 8, 0, False) for t in times])).height == 0


def test_hourly_slots_and_unsupported_durations() -> None:
    per_minute = afternoon(0, {}) | afternoon(1, {m: 10 for m in range(60)}) | afternoon(2, {})

    flagged = frozen_feed_slots(counts(per_minute), slot="1h", min_versions=36)

    tuesday_noon = MONDAY + timedelta(days=1, hours=12)
    assert flagged.select("slot_start", "reason").rows() == [
        (tuesday_noon, "frozen"),
        (tuesday_noon + timedelta(hours=1), "catch_up"),
    ]
    with pytest.raises(ValueError, match="unsupported duration"):
        frozen_feed_slots(counts(per_minute), slot="30s")
