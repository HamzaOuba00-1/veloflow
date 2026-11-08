# Test fixtures

## `gbfs/`

Trimmed copies of real responses from the Vélib' Métropole GBFS feed, recorded
on 4 November 2026 at 16:48 (Paris time) by `scripts/explore_gbfs.py`.

Only the envelope and nine stations are kept, each chosen for a case the code
must handle:

| Station code | Why it is here |
|---|---|
| 16107 | Ordinary station in service |
| 13123 | More bikes + docks than its declared capacity |
| 13128 | In service but `last_reported` 16 hours old |
| 8116 | Installed but closed |
| 21502 | Not installed |
| 60001 | Very large station (capacity 410) |
| 8017 | Capacity 0 |
| 5022 | Full (no free dock) |
| 11104 | Empty (no bike) |

Station objects are copied verbatim; nothing was edited by hand.

**Data licence.** Source: Vélib' Métropole, published under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
These extracts remain under the ODbL.

## `calendar/`

`calendrier_scolaire_paris.json`: records of the official school calendar for
the Paris academy (zone C), school years 2025-2026 to 2027-2028, exported on
8 November 2026 from `data.education.gouv.fr` (dataset
`fr-en-calendrier-scolaire`). Copied verbatim; it contains the two anomalies the
parser must handle (a zero-length Ascension bridge in 2027, a "Début des
Vacances d'Été" record without an end).

**Data licence.** Ministère de l'Éducation nationale, published under the
[Licence Ouverte / Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/).
