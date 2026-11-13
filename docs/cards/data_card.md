# Data card

What VéloFlow collects, how, what is wrong with it, and under which licences.
No personal data: every record describes a station, never a rider.

## Sources

| Data | Source | Frequency | Licence |
|---|---|---|---|
| Station status: mechanical bikes, e-bikes, free docks, flags (installed, renting, returning) | Vélib' Métropole GBFS feed `station_status` | every 30 s, since 8 Nov 2026 | ODbL |
| Station reference: name, position, declared capacity | GBFS `station_information` | hourly | ODbL |
| Weather forecast: temperature, precipitation, wind, 15-minute steps | Open-Meteo (Météo-France models), on a grid over Paris | hourly snapshot | CC BY 4.0 |
| School holidays (zone C), public holidays | Éducation nationale open data; `holidays` package | refreshed by hand | Licence Ouverte 2.0 |
| Bike counters (exploration only, not used by the model) | Ville de Paris | yearly files | ODbL |

## Collection and storage

- A collector on an e2-micro VM fetches the feed every 30 seconds on a fixed
  grid and appends each response, unchanged, to a gzip JSON-lines archive
  (bronze); a fingerprint skips identical contents
  ([deployment](../deployment.md)).
- Every night the day is compacted to Parquet (silver), one row per station
  and feed version: about 1 to 3 MB a day against about 43 MB of raw gzip
  ([storage](../data/storage.md)).
- The archive is pulled to the laptop for exploration, training and
  backtests (`infra/sync/pull_data.ps1`). It is not in Git.

## Known problems, measured ([quality](../data/quality.md))

| Problem | Measured on 8-9 Nov | Handling |
|---|---|---|
| Versions from the past (feed time goes back) | 0.9 % of versions | R1: dropped when read, counted |
| Declared capacity wrong | 93 stations above it at some point | R2 (planned); the model caps with what was seen |
| Closed stations | 3 not installed, 36 neither renting nor returning | R3: kept as features, excluded from targets |
| Frozen feed (published on time, content stopped) | one incident of 2.5 h on 9 Nov | R4a: masked from targets and evaluation |
| Frozen stations | 2 stations without change over 24 h | R4b: flagged, left out of evaluation |
| Missing or degraded versions | 9 Nov: 20 degraded, 9 never received | R6: daily coverage report |

Rules are pure functions applied when the data is read: changing one never
rewrites the archive.

## Gaps in the history

- 5 November: a 17-minute trial of the collector; 6-7 November: nothing.
  Continuous collection starts on 8 November 2026 at 13:20 UTC.
- 12 November, about 22:20 UTC: the VM was stopped for about a minute to
  attach its service account.
- Weather history is downloaded on demand: days without it have null weather
  features.

## Biases and representativeness

- **Season**: November only. Summer, school holidays and events are absent.
- **Paris and suburbs**: 522 of about 1,500 stations are outside Paris; they
  are measured as a subgroup in every evaluation.
- **Censored demand**: an empty station hides how many riders wanted a bike;
  a full one, how many wanted to return one.

## Use and redistribution

Derived data (the forecast document, reports) may be redistributed under the
ODbL with attribution to Vélib' Métropole, and to Open-Meteo for anything
containing weather. The forecast document is public:
`https://storage.googleapis.com/veloflow-forecasts-513808/forecasts.json`.
