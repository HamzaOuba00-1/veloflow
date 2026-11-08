"""Official school holidays for Paris (zone C).

Source: dataset ``fr-en-calendrier-scolaire`` of the French Ministry of
Education (Licence Ouverte / Etalab 2.0). The calendar changes once a year, so
it is not fetched at run time: ``veloflow-refresh-school-holidays`` rewrites a
small CSV shipped inside the package, and every run reads that versioned copy
(reproducible, works offline, on the VM and in CI).

How the source encodes dates, measured on 8 November 2026 (75 Paris records):

* ``start_date`` and ``end_date`` are midnights in Paris time, stored in UTC
  (``T23:00Z`` in winter, ``T22:00Z`` in summer). Taking the UTC date would
  shift every holiday one day early.
* ``end_date`` is the day classes resume: holiday days are ``[start, end)``.
* Summer appears twice (pupils and teachers); pupils are kept.
* Anomalies: a zero-length period (Ascension 2027) and a "Début des Vacances
  d'Été" record whose end is not published yet.
"""

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

PARIS = ZoneInfo("Europe/Paris")
SOURCE_URL = (
    "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "fr-en-calendrier-scolaire/exports/json?where=location%3D%22Paris%22"
)
REFERENCE_FILE = "school_holidays_zone_c.csv"
# Teachers' rows (summer only) are dropped; "-" covers every other period.
KEPT_POPULATIONS = {"-", "Élèves"}
SUMMER = "Vacances d'Été"


class SchoolCalendarError(ValueError):
    """The source records cannot be turned into a reliable calendar."""


@dataclass(frozen=True, slots=True)
class SchoolHoliday:
    """One holiday period, as inclusive local dates."""

    name: str
    school_year: str
    first_day: date
    last_day: date

    def contains(self, day: date) -> bool:
        """Whether ``day`` is a holiday day of this period."""
        return self.first_day <= day <= self.last_day


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str
    population: str
    start_date: AwareDatetime
    end_date: AwareDatetime
    annee_scolaire: str


def _paris_date(moment: datetime) -> date:
    """Local date in Paris. Never ``moment.date()``: that is the UTC date."""
    return moment.astimezone(PARIS).date()


def parse_school_calendar(records: list[dict[str, Any]]) -> list[SchoolHoliday]:
    """Turn raw source records into holiday periods, sorted by first day.

    Raises:
        SchoolCalendarError: a record misses a field or has a naive timestamp.
    """
    periods: set[SchoolHoliday] = set()
    for raw in records:
        try:
            record = _Record.model_validate(raw)
        except ValidationError as error:
            raise SchoolCalendarError(f"invalid school calendar record: {raw}") from error

        if record.population not in KEPT_POPULATIONS:
            continue
        if record.description.startswith("Début"):
            # Only the start of the period is known; nothing reliable to keep.
            logger.info(
                "skipping %r (%s): end not published", record.description, record.annee_scolaire
            )
            continue

        first_day = _paris_date(record.start_date)
        resume_day = _paris_date(record.end_date)
        if resume_day <= first_day:
            # Observed for Ascension 2027: keep the one day the source gives,
            # rather than inventing when classes resume.
            logger.warning(
                "%r (%s) has no duration in the source: kept as the single day %s",
                record.description,
                record.annee_scolaire,
                first_day,
            )
            resume_day = first_day + timedelta(days=1)

        periods.add(
            SchoolHoliday(
                name=record.description,
                school_year=record.annee_scolaire,
                first_day=first_day,
                last_day=resume_day - timedelta(days=1),
            )
        )
    return sorted(periods, key=lambda period: period.first_day)


def write_reference(periods: list[SchoolHoliday], path: Path) -> None:
    """Write the periods as the versioned CSV reference."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["name", "school_year", "first_day", "last_day"])
        for period in periods:
            writer.writerow(
                [
                    period.name,
                    period.school_year,
                    period.first_day.isoformat(),
                    period.last_day.isoformat(),
                ]
            )


def load_reference(path: Path | None = None) -> list[SchoolHoliday]:
    """Read the CSV reference; by default the copy shipped in the package."""
    if path is None:
        text = (
            files("veloflow.context").joinpath("data", REFERENCE_FILE).read_text(encoding="utf-8")
        )
    else:
        text = path.read_text(encoding="utf-8")
    return [
        SchoolHoliday(
            name=row["name"],
            school_year=row["school_year"],
            first_day=date.fromisoformat(row["first_day"]),
            last_day=date.fromisoformat(row["last_day"]),
        )
        for row in csv.DictReader(text.splitlines())
    ]


def covered_until(periods: list[SchoolHoliday]) -> date:
    """Last day the calendar is known to be complete: the end of the last summer.

    After it, later school years may be partially published, so "not a holiday"
    could simply mean "not published yet".
    """
    summers = [period.last_day for period in periods if period.name == SUMMER]
    if not summers:
        raise SchoolCalendarError("no summer holiday in the reference")
    return max(summers)


def fetch_records(url: str = SOURCE_URL, *, timeout_s: float = 30.0) -> list[dict[str, Any]]:
    """Download the raw records of the source dataset."""
    response = httpx.get(url, timeout=timeout_s, follow_redirects=True)
    response.raise_for_status()
    records = response.json()
    if not isinstance(records, list):
        raise SchoolCalendarError("the export did not return a list of records")
    return records


def main(argv: list[str] | None = None) -> int:
    """Refresh the versioned CSV reference from the official source."""
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Refresh the school holidays reference.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/veloflow/context/data") / REFERENCE_FILE,
        help="CSV to write (default: the copy shipped in the package)",
    )
    args = parser.parse_args(argv)

    periods = parse_school_calendar(fetch_records())
    write_reference(periods, args.output)
    logger.info(
        "%d periods written to %s, complete until %s",
        len(periods),
        args.output,
        covered_until(periods),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
