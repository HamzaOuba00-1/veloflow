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

## What the files look like (measured on 2025)

| Observation | Handling |
|---|---|
| 21 MB zipped, 1.93 GB unzipped (every row repeats ten photo links) | The CSV is extracted to a temporary folder, converted, and deleted; only the zip and a 5 MB Parquet file stay |
| Latin-1, not UTF-8 | Only identifiers, numbers and coordinates are kept |
| Counts of 1,000 and more written with a non-breaking space (`1 022`): 6,694 values | Non-digits are removed before conversion |
| Timestamps carry their UTC offset (`2025-01-01 00:00:00.000 +0100`) | Converted to UTC |
| The year runs from 00:00 to 23:00 local time | The timestamp **starts** the counted hour (`hour_start`), unlike Open-Meteo, whose accumulated values end their interval |
| 8,759 rows a year without any identifier or coordinates, counts up to 13,057 | Not a counter: dropped |
| 2025: 171 counters on 89 sites, 1.49 million rows, no duplicate | — |

## Usage

```powershell
uv run veloflow-counters --data-dir data --years 2016-2025
```

```
data/raw/bike_counts/bike_counts_<year>.zip                  as downloaded
data/silver/bike_counts/year=<year>/bike_counts.parquet      counter_id, site_id, lat, lon, hour_start (UTC), count
```

Years already converted are skipped (`--force` converts them again); a zip
already downloaded is not downloaded again. A year whose format differs stops
with an explicit error instead of producing wrong numbers.
