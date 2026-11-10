"""Tests for rule R1 (versions in feed-time order) and rule R3 (operational)."""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from veloflow.quality.versions import select_versions, with_operational
from veloflow.storage.compaction import STATUS_SCHEMA

T0 = datetime(2026, 11, 9, 18, 40, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def versions(*specs: tuple[float, float, int]) -> pl.DataFrame:
    """One version per spec (fetch second, feed second, bikes), two stations each."""
    rows = [
        (at(feed), at(fetched), station, bikes, 0, 10, True, True, True, at(feed))
        for fetched, feed, bikes in specs
        for station in (1, 2)
    ]
    return pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row")


def kept_fetches(frame: pl.DataFrame) -> list[datetime]:
    return frame["fetched_at"].unique(maintain_order=True).to_list()


def test_versions_moving_forward_are_all_kept() -> None:
    result = select_versions(versions((8, 0, 5), (68, 60, 6), (128, 120, 7)))

    assert (result.kept, result.stale, result.replaced_ties) == (3, 0, 0)
    assert result.frame.height == 6


def test_version_going_back_in_time_is_stale() -> None:
    # Pattern of 9 Nov 2026 18:43: a server publishes a state 1 s older.
    result = select_versions(versions((8, 60, 5), (38, 59, 9), (68, 120, 6)))

    assert (result.kept, result.stale) == (2, 1)
    assert kept_fetches(result.frame) == [at(8), at(68)]
    assert 9 not in result.frame["mechanical"].to_list()


def test_on_a_feed_time_tie_the_later_fetch_wins() -> None:
    # Pattern of 9 Nov 2026 19:48:01: same feed time, 116 stations differ.
    result = select_versions(versions((8, 60, 5), (38, 60, 9), (68, 120, 6)))

    assert (result.kept, result.stale, result.replaced_ties) == (2, 0, 1)
    assert kept_fetches(result.frame) == [at(38), at(68)]


def test_rows_are_ordered_by_fetch_then_station() -> None:
    frame = versions((68, 120, 6), (8, 60, 5)).sample(fraction=1.0, shuffle=True, seed=1)

    result = select_versions(frame)

    assert result.frame.select("fetched_at", "station_id").rows() == [
        (at(8), 1),
        (at(8), 2),
        (at(68), 1),
        (at(68), 2),
    ]


@pytest.mark.parametrize("feed", [-1, 0], ids=["older", "same"])
def test_versions_not_after_the_previous_day_are_stale(feed: float) -> None:
    result = select_versions(versions((8, feed, 5), (68, 60, 6)), after=at(0))

    assert (result.kept, result.stale) == (1, 1)
    assert kept_fetches(result.frame) == [at(68)]


def test_empty_frame() -> None:
    result = select_versions(pl.DataFrame(schema=STATUS_SCHEMA))

    assert (result.kept, result.stale, result.frame.height) == (0, 0, 0)


@pytest.mark.parametrize(
    ("installed", "renting", "returning", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_operational_needs_all_three_flags(
    installed: bool, renting: bool, returning: bool, expected: bool
) -> None:
    frame = versions((8, 0, 5)).with_columns(
        is_installed=pl.lit(installed),
        is_renting=pl.lit(renting),
        is_returning=pl.lit(returning),
    )

    assert with_operational(frame)["operational"].to_list() == [expected, expected]
