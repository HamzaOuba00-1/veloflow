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
