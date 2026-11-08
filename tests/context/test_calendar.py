"""Tests for the daily calendar."""

from datetime import date

import polars as pl
import pytest

from veloflow.context.calendar import CALENDAR_SCHEMA, daily_calendar


def row(frame: pl.DataFrame, day: date) -> dict[str, object]:
    return frame.filter(pl.col("date") == day).row(0, named=True)


def test_christmas_2026_period() -> None:
    frame = daily_calendar(date(2026, 12, 17), date(2027, 1, 5))

    assert dict(frame.schema) == CALENDAR_SCHEMA
    assert frame.height == 20
    friday = row(frame, date(2026, 12, 18))
    assert (friday["is_weekend"], friday["is_school_holiday"]) == (False, False)
    saturday = row(frame, date(2026, 12, 19))
    assert (saturday["is_weekend"], saturday["school_holiday_name"]) == (True, "Vacances de Noël")
    christmas = row(frame, date(2026, 12, 25))
    assert (christmas["public_holiday_name"], christmas["is_school_holiday"]) == ("Noël", True)
    assert row(frame, date(2027, 1, 1))["is_public_holiday"] is True
    back_to_school = row(frame, date(2027, 1, 4))
    assert (back_to_school["weekday"], back_to_school["is_school_holiday"]) == (0, False)


def test_ascension_2027_is_a_public_holiday_and_the_bridge_a_school_holiday() -> None:
    frame = daily_calendar(date(2027, 5, 6), date(2027, 5, 7))

    assert row(frame, date(2027, 5, 6))["is_public_holiday"] is True
    assert row(frame, date(2027, 5, 7))["school_holiday_name"] == "Pont de l'Ascension"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 11, 9), date(2026, 11, 8)),  # empty
        (date(2016, 1, 1), date(2016, 1, 2)),  # before the source
        (date(2027, 9, 1), date(2027, 9, 10)),  # after the last complete year
    ],
)
def test_ranges_outside_the_known_calendar_are_refused(start: date, end: date) -> None:
    with pytest.raises(ValueError, match=r"empty range|school calendar known"):
        daily_calendar(start, end)
