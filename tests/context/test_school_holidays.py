"""Tests for the school holidays parser, on real records of the official source."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from veloflow.context import school_holidays as module
from veloflow.context.school_holidays import (
    SchoolCalendarError,
    SchoolHoliday,
    covered_until,
    load_reference,
    parse_school_calendar,
    write_reference,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "calendar" / "calendrier_scolaire_paris.json"


def records() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def by_name(periods: list[SchoolHoliday], name: str, year: str) -> SchoolHoliday:
    return next(p for p in periods if p.name == name and p.school_year == year)


def test_dates_are_paris_local_and_end_is_the_resumption_day() -> None:
    periods = parse_school_calendar(records())

    christmas = by_name(periods, "Vacances de Noël", "2025-2026")
    # Officially: from Saturday 20 December 2025, classes resume Monday 5 January.
    assert (christmas.first_day, christmas.last_day) == (date(2025, 12, 20), date(2026, 1, 4))
    autumn = by_name(periods, "Vacances de la Toussaint", "2026-2027")
    assert (autumn.first_day, autumn.last_day) == (date(2026, 10, 17), date(2026, 11, 1))


def test_every_period_starts_on_a_saturday_in_winter_and_summer_time() -> None:
    """Taking the UTC date instead would make them all start on a Friday."""
    periods = parse_school_calendar(records())

    starts = {p.first_day.weekday() for p in periods if p.name != "Pont de l'Ascension"}
    assert starts == {5}


def test_summer_keeps_pupils_not_teachers() -> None:
    summer = by_name(parse_school_calendar(records()), "Vacances d'Été", "2025-2026")

    assert summer.last_day == date(2026, 8, 31)  # pupils resume 1 Sept; teachers on 31 Aug


def test_zero_length_period_becomes_one_day_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ascension = by_name(parse_school_calendar(records()), "Pont de l'Ascension", "2026-2027")

    assert ascension.first_day == ascension.last_day == date(2027, 5, 7)
    assert "has no duration in the source" in caplog.text


def test_start_only_records_are_skipped() -> None:
    names = {p.name for p in parse_school_calendar(records())}

    assert not any(name.startswith("Début") for name in names)


def test_duplicates_collapse_and_order_is_chronological() -> None:
    periods = parse_school_calendar(records() + records())

    assert len(periods) == len(parse_school_calendar(records()))
    assert [p.first_day for p in periods] == sorted(p.first_day for p in periods)


@pytest.mark.parametrize(
    "broken",
    [
        {"description": "x", "population": "-", "annee_scolaire": "y"},  # no dates
        {
            "description": "x",
            "population": "-",
            "annee_scolaire": "y",
            "start_date": "2026-10-17T00:00:00",  # naive: ambiguous, refused
            "end_date": "2026-11-02T00:00:00",
        },
    ],
)
def test_invalid_records_are_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(SchoolCalendarError):
        parse_school_calendar([broken])


def test_reference_round_trip_and_coverage(tmp_path: Path) -> None:
    periods = parse_school_calendar(records())
    path = tmp_path / "ref.csv"

    write_reference(periods, path)

    assert load_reference(path) == periods
    assert covered_until(periods) == date(2027, 9, 1)  # pupils resume 2 Sept 2027


def test_coverage_needs_a_summer() -> None:
    with pytest.raises(SchoolCalendarError):
        covered_until(
            [SchoolHoliday("Vacances de Noël", "y", date(2026, 12, 19), date(2027, 1, 3))]
        )


def test_packaged_reference_covers_the_project_period() -> None:
    periods = load_reference()

    assert covered_until(periods) >= date(2027, 3, 31)
    assert by_name(periods, "Vacances de Noël", "2026-2027").first_day == date(2026, 12, 19)


def test_fetch_records_rejects_a_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"error": "x"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(SchoolCalendarError):
        module.fetch_records()


def test_refresh_cli_writes_the_reference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(module, "fetch_records", records)
    output = tmp_path / "ref.csv"

    assert module.main(["--output", str(output)]) == 0
    assert load_reference(output) == parse_school_calendar(records())
