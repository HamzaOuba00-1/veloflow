# Bike counts of the City of Paris

Our collection started in November 2026: by the end of the project it will
hold a few months and no summer. The City of Paris has counted bikes every hour
since 2016 with permanent counters on cycle lanes and bus lanes. These counts
measure how cycling demand in Paris reacts to the weekday, public and school
holidays, rain, temperature and seasons, effects that our own data cannot show
yet (exploration step 3.4).

They count every bike passing a counter, not Vélib' rentals: they give the
size and shape of the effects, not station availability.

## Source

"Comptage vélo - Historique - Données Compteurs et Sites de comptage", Ville de
Paris, Direction de la Voirie et des Déplacements, published on
opendata.paris.fr under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
One zipped CSV per year (2016-2025); the current year is in a separate dataset
over a rolling 13 months.

## What the files look like (measured on 2016-2025, 11 November 2026)

The yearly files do not share one format:

| Year | Separator | Encoding | Timestamp | Count | Counters | Rows kept |
|---|---|---|---|---|---|---|
| 2016 | `;` | UTF-8 | `2016-01-01T00:00:00` (local, no offset) | `3` | 46 | 404,018 |
| 2017 | `;` | UTF-8 | local, no offset | `4` | 64 | 560,576 |
| 2018 | `;` | UTF-8 | `2018-11-29T01:00:00+01:00` | `4` | 45 | 157,813 |
| 2019 | `;` | UTF-8 | with offset | `0` | 81 | 436,690 |
| 2020 | `;` | UTF-8 | with offset | `12.0` | 96 | 784,339 |
| 2021 | `;` | UTF-8 | local, no offset | `2.0` | 167 | 1,462,753 |
| 2022 | `;` | UTF-8 | local, no offset | `0` | 168 | 1,471,512 |
| 2023 | `;` | UTF-8 with BOM, 7 extra columns | with offset | `4` | 101 | 860,190 |
| 2024 | `;`, quoted | UTF-8 | `2024-01-02 19:00:00.000 +0100` | `155.0` | 178 | 1,496,857 |
| 2025 | `,`, quoted | **Latin-1** | `2025-05-06 14:00:00.000 +0200` | `1 022` | 171 | 1,494,195 |

How each difference is handled:

| Observation | Handling |
|---|---|
| Zips of 2018-2020 also hold macOS metadata (`__MACOSX/._…csv`) | Ignored |
| 21 MB zipped, up to 1.9 GB unzipped (every row repeats photo links) | The CSV is extracted to a temporary folder, converted, deleted; only the zip and a Parquet file of 0.2-2.7 MB stay (14 MB for ten years) |
| Counts `12.0` and `1 022` (non-breaking space) | Digits and the decimal point are kept; a count that is not a whole number stops the conversion |
| Timestamps with offset | Converted to UTC |
| Local times without offset (2016, 2017, 2021, 2022) | Read as Paris time. Peak hours stay at 08-09 and 18-19 in January and July, which rules out UTC. These files list every hour of every day: the hour that does not exist when clocks go forward (one row per counter, 46 to 168 a year) is dropped, and the hour that occurs twice when clocks go back appears once and is read as the first |
| 2018 and 2019 list the spring clock-change hour twice per counter, once with 0 (12 and 39 cases) | The larger count is kept |
| In every year the first hour is 00:00 local | The timestamp **starts** the counted hour (`hour_start`), unlike Open-Meteo, whose accumulated values end their interval |
| Rows without any identifier or coordinates (2025: 8,759, counts up to 13,057) | Not a counter: dropped |

The counter network changes a lot: 45 counters in 2018, 178 in 2024, and some
years are incomplete (2018 holds about 3,500 hours per counter; 2023 starts on
1 January at 04:00). Analyses must use a stable panel of counters. Some hourly
counts are suspiciously high (6,390 in 2022, 8,190 in 2023, against about
2,000 at most in other years) and need an outlier check.

## Usage

```powershell
uv run veloflow-counters --data-dir data --years 2016-2025
```

```
data/raw/bike_counts/bike_counts_<year>.zip                  as downloaded
data/silver/bike_counts/year=<year>/bike_counts.parquet      counter_id, site_id, lat, lon, hour_start (UTC), count
```

Years already converted are skipped (`--force` converts them again); a zip
already downloaded is not downloaded again. A year whose format is not one of the above
stops with an explicit error instead of producing wrong numbers.
