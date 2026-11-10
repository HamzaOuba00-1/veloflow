# Data quality

The silver layer keeps every valid feed version as received. The rules below
live in `veloflow.quality` as pure functions applied when the data is read, so
that changing a rule never requires rebuilding the silver files.

## Diagnosis (8-9 November 2026)

Measured on the first 36 hours of collection: 2,051 versions of
`station_status`, the station reference, and the raw archive on the VM.

| Finding | Measure | Rule |
|---|---|---|
| Versions from the past: the feed time goes back 1-10 s, or repeats | 18 of 2,051 versions (0.9 %), about one an hour | R1 |
| Declared capacity is wrong for some stations | 93 stations above it at some point, some all day (declared 48, seen 78) | R2 |
| Closed stations | 3 not installed, 36 neither renting nor returning (9 Nov) | R3 |
| Open stations that never change | 2 stations with no change over 24 h; a typical station changes 107 times a day | R4 |
| The station reference moves | 2 stations added, 2 capacity changes, 1 station moved in 2.5 days | R5 |
| Unusable or missing versions | 9 Nov: 1,418 usable, 20 degraded (published by the source), ~2 minutes silent | R6 |

A version from the past differs from the next version about twice as much as
the previous version does (for example 424 stations against 260 on 9 November
at 18:43): it is a stale state served by a lagging server, not a newer one.

Empty and full stations are rare events: 5.7 % and 2.9 % of the observations
of operational stations on 9 November.

## Rules

| Rule | Status | Definition |
|---|---|---|
| R1 | Implemented (`quality.versions.select_versions`) | Keep a version only if its feed time is strictly later than the last kept one; on a tie, the later fetch replaces the earlier one. Removed versions are counted, never deleted from the raw archive |
| R2 | Planned | Effective capacity: the larger of the declared capacity and the maximum of bikes + docks seen over the **previous** 7 days only (no look-ahead) |
| R3 | Implemented (`quality.versions.with_operational`) | `operational` = installed and renting and returning. Non-operational moments are excluded from the targets, kept as features |
| R4 | Planned | Suspected frozen station: operational with no change for N hours; N to calibrate on more days, with an error rate measured on a sample |
| R5 | Planned | Station reference history: one row per station and period of validity |
| R6 | Implemented (`quality.coverage`, `veloflow-coverage`) | Daily coverage report that separates source failures from silences |

### R1 on real data

| Day | Kept | Stale (feed time went back) | Ties replaced |
|---|---|---|---|
| 8 Nov (from 13:20 UTC) | 625 | 7 | 1 |
| 9 Nov | 1,408 | 8 | 2 |

On the three ties observed, the later fetch was each time the one consistent
with the following version. Removing the stale versions takes out 0.7 % of the
total bike movement measured on 9 November: movement that never happened.

### R6: two kinds of missed versions

The specification asks for less than 1 % of missed minutes, gaps listed and
explained. A missing usable version has two causes, reported apart:

- **source failures**: the source published something unusable (a degraded
  version or a body that does not parse). It was received and archived as
  evidence; nothing on our side can fix it;
- **silent gaps**: more than 120 s between two archived responses (the feed
  publishes every 59-61 s), so at least one version was never received:
  collector down or source silent. The journal of the collector tells which.

On 9 November: 20 source failures (1.4 %) and about 2 silent minutes. Raw files
written before the collector learned to flag degraded versions (5 November)
report them as `ok`.

```powershell
uv run veloflow-coverage --data-dir data --date 2026-11-09   # one day
uv run veloflow-coverage --data-dir data --all               # every raw day
```

Reports go to `data/quality/coverage/coverage_<day>.json`. They need the raw
archive, so they run on the VM every night right after the compaction
(`veloflow-compact.service`), and `infra/sync/pull_data.ps1` brings them to the
laptop.
