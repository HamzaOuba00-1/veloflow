# Calendar data

`veloflow.context.calendar.daily_calendar(start, end)` returns one row per local
date (Paris) with the weekday, weekends, public holidays and school holidays.

| Column | Source |
|---|---|
| `is_public_holiday`, `public_holiday_name` | Computed by the [`holidays`](https://pypi.org/project/holidays/) library, metropolitan France (no Alsace-Moselle extras) |
| `is_school_holiday`, `school_holiday_name` | Official school calendar, Paris academy (zone C) |

## School holidays: source and interpretation

Dataset `fr-en-calendrier-scolaire` of the Ministry of Education, published on
`data.education.gouv.fr` under the Licence Ouverte / Etalab 2.0. It changes once
a year, so it is **not fetched at run time**: a CSV copy is versioned in the
package (`src/veloflow/context/data/school_holidays_zone_c.csv`) and refreshed
by hand with `uv run veloflow-refresh-school-holidays`.

Rules, checked on 8 November 2026 against official dates (75 Paris records):

| Observation in the source | Rule applied |
|---|---|
| Dates are Paris midnights stored in UTC (`T23:00Z` in winter, `T22:00Z` in summer) | Convert to Europe/Paris before taking the date. The UTC date would start every holiday one day early. |
| `end_date` is the day classes resume | Holiday days are `[start, end)` |
| Summer is listed for pupils and for teachers | Pupils are kept |
| Ascension 2027 has the same start and end | Kept as a single day (7 May 2027), with a warning; no resumption day is invented |
| A "Début des Vacances d'Été" record has no end | Skipped |

The reference is complete until **1 September 2027** (end of the last published
summer). `daily_calendar` refuses dates outside the known range: an unpublished
holiday would otherwise read as a school day without any error.
