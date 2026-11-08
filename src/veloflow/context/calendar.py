"""Daily calendar for Paris: weekends, public holidays, school holidays.

One row per local date, ready to be joined to station observations by date.
"""

from datetime import date, timedelta

import holidays
import polars as pl

from veloflow.context.school_holidays import SchoolHoliday, covered_until, load_reference

CALENDAR_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date(),
    "weekday": pl.Int8(),  # 0 = Monday
    "is_weekend": pl.Boolean(),
    "is_public_holiday": pl.Boolean(),
    "public_holiday_name": pl.String(),
    "is_school_holiday": pl.Boolean(),
    "school_holiday_name": pl.String(),
}


def daily_calendar(
    start: date, end: date, *, school_holidays: list[SchoolHoliday] | None = None
) -> pl.DataFrame:
    """Calendar features for every local date from ``start`` to ``end`` included.

    Public holidays are computed for metropolitan France (no Alsace-Moselle
    extras); school holidays come from the versioned zone C reference.

    Raises:
        ValueError: the range is empty or reaches beyond the published school
            calendar (a missing holiday would silently read as a school day).
    """
    if end < start:
        raise ValueError(f"empty range: {start} > {end}")
    periods = load_reference() if school_holidays is None else school_holidays
    first_known = min(period.first_day for period in periods)
    last_known = covered_until(periods)
    if start < first_known or end > last_known:
        raise ValueError(
            f"school calendar known from {first_known} to {last_known}, asked {start} to {end}"
        )

    public = holidays.country_holidays("FR", years=range(start.year, end.year + 1), language="fr")
    rows = []
    day = start
    while day <= end:
        school = next((period for period in periods if period.contains(day)), None)
        public_name = public.get(day)
        rows.append(
            (
                day,
                day.weekday(),
                day.weekday() >= 5,
                public_name is not None,
                public_name,
                school is not None,
                school.name if school else None,
            )
        )
        day += timedelta(days=1)
    return pl.DataFrame(rows, schema=CALENDAR_SCHEMA, orient="row")
