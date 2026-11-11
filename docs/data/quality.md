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
| Unusable or missing versions | 9 Nov: 1,418 usable, 20 degraded (published by the source), 9 versions never received | R6 |

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
| R4a | Implemented (`quality.freeze.frozen_feed_slots`) | Frozen feed: a quarter hour where the whole network changes less than 30 % of the usual amount, plus the quarter hour that catches up. Excluded from targets and evaluation |
| R4b | Implemented, threshold provisional (`quality.freeze.suspected_frozen_stations`) | Suspected frozen station: operational with no change for 12 daytime hours, flagged from the 12th hour until its next change |
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

### R4: frozen feed and frozen stations

A feed can keep publishing on time while its content stops moving. R6 does not
see that, since every version arrives. R4 looks at the content instead.

**R4a, frozen feed.** For each version, count the operational stations whose
state (mechanical bikes, e-bikes, docks) changed since their previous version,
and sum per quarter hour. The usual amount for a quarter hour is the median of
the same local time on the *other* days of the same type (working day or
weekend): the day under test never sets its own reference. A quarter hour is
frozen when it gets less than 30 % of that amount. Calm quarter hours (usual
amount below 300 changes, i.e. nights) and quarter hours with fewer than 9
versions (coverage gaps, already reported by R6) are never judged. A quarter
hour between two frozen ones is frozen too, and the first normal quarter hour
after a freeze is labelled `catch_up`: it releases the movement that piled up,
so it is no more usable as a target than the freeze itself.

Measured on 8-10 November, one incident, on 9 November:

| Quarter hour (UTC) | Changes | Usual | Label |
|---|---|---|---|
| 12:45 | 490 | 2,425 | frozen |
| 13:00 | 199 | 2,230 | frozen |
| 13:15 | 248 | 1,679 | frozen |
| 13:30 | 191 | 1,402 | frozen |
| 13:45 | 299 | 1,588 | frozen |
| 14:00 | 502 | 1,595 | frozen |
| 14:15 | 287 | 1,964 | frozen |
| 14:30 | 220 | 1,787 | frozen |
| 14:45 | 248 | 1,935 | frozen |
| 15:00 | 3,173 | 2,046 | catch_up |

The freeze starts 25 minutes before the silent gaps of R6 (13:10-15:09 UTC):
for those 25 minutes the feed was publishing versions on time, with stale
content. Without R4a, a model would learn that a busy weekday afternoon
barely moves, then that 3,000 changes happen in one quarter hour.

During the incident, quarter hours got 9 % to 31.5 % of the usual amount;
the catch-up got 155 %. Only 14:00 (31.5 %) is above the threshold, and it is
flagged because it sits between two frozen quarter hours. The 30 % threshold
comes from a single incident, so it will be checked against new ones as they
come.

**R4b, suspected frozen station.** For an operational station, a run is a
stretch of versions with the same state. Only daytime counts (07:00-22:00
Paris time): a station that does not move at night is normal. The station is
suspected from the moment its run reaches 12 daytime hours, until its state
changes (or the data ends). A typical station changes about 107 times a day,
so 12 daytime hours without a single change is very unlikely for a working
station. A gap of more than an hour between two versions of a station ends
its run: nothing was observed during the gap, so it cannot count as time
without change. Without this, the 17-minute trial run of 5 November turned
25 stations that merely had the same state on 5 and 8 November into
"frozen for 75 hours" (found by the backtest report, fixed before it counted).

Measured on 8-10 November: 8 periods on 7 stations (out of about 1,500).

| Station | Suspected from (UTC) | Until (UTC) |
|---|---|---|
| Lacépède - Monge | 9 Nov 10:26 | 9 Nov 22:57 |
| Saint-Germain - Verdun | 9 Nov 10:26 | end of data (10 Nov) |
| 11 Novembre 1918 - 8 Mai 1945 | 9 Nov 18:06 | 9 Nov 18:39 |
| Bouvier - Carnot | 9 Nov 19:05 | 10 Nov 07:50 |
| Gare RER les Saules - Chandigarh | 9 Nov 19:12 | 10 Nov 06:24 |
| Versailles - Jardy | 10 Nov 14:19 | 10 Nov 16:14 |
| 11 Novembre 1918 - 8 Mai 1945 | 10 Nov 19:05 | 10 Nov 22:01 |
| Pavé des Gardes - Charles Vaillant | 10 Nov 19:31 | end of data (10 Nov) |

The first two runs start with the first collected version (8 November,
13:20 UTC): those stations were already unchanged before collection began.

Nothing proves yet that these stations were really frozen: a quiet station
in the suburbs can go 12 hours without a rental. The precision of R4b
will be measured after two weeks of data, in two ways:

- automatically: stations later declared closed by the operator (R3) were
  most likely frozen before;
- by hand: a sample of suspected periods checked against the Vélib' app while
  they are happening.

Until then, R4b periods are kept as a separate mask, so the backtest can
report results with and without them.

### R6: two kinds of missed versions

The specification asks for less than 1 % of missed minutes, gaps listed and
explained. A missing usable version has two causes, reported apart:

- **source failures**: the source published something unusable (a degraded
  version or a body that does not parse). It was received and archived as
  evidence; nothing on our side can fix it;
- **silent gaps**: more than 120 s between two archived responses (the feed
  publishes every 59-61 s), so at least one version was never received:
  collector down or source silent. The journal of the collector tells which.

Measured on the VM (`veloflow-coverage --all`, 15 s and 146 MB for three days):

| Day | Expected | Usable | Source failures | Silent |
|---|---|---|---|---|
| 8 Nov (from 13:20 UTC) | 642 | 98.60 % | 1.40 % | 0.00 % |
| 9 Nov | 1,440 | 98.47 % | 1.39 % | 0.63 % (9 versions, 5 gaps) |
| 10 Nov (partial) | 968 | 98.45 % | 1.55 % | 0.00 % |

The shares can add up to slightly more than 100 %: a minute sometimes holds two
responses (the stale versions of rule R1), so counting responses against
minutes would hide gaps. The gaps are measured between consecutive responses
instead.

**The 5 gaps of 9 November are explained.** They all fall between 13:10 and
15:09 UTC. Over that window the collector logged no failed request, only the
two degraded versions it rejected (13:56 and 14:56, all 1,522 stations without
a station code), and it kept pinging the external monitor after healthy polls.
It was polling; the feed simply published no new state. All 9 missing versions
are therefore on the source side, and the collector's own miss rate that day is
0 %.

Both degraded versions of that window came at minute 56 of the hour, with
every station losing its code. Whether this is a schedule on the operator's
side will be checked over more days.

Raw files written before the collector learned to flag degraded versions
(5 November) report them as `ok`.

```powershell
uv run veloflow-coverage --data-dir data --date 2026-11-09   # one day
uv run veloflow-coverage --data-dir data --all               # every raw day
```

Reports go to `data/quality/coverage/coverage_<day>.json`. They need the raw
archive, so they run on the VM every night right after the compaction
(`veloflow-compact.service`), and `infra/sync/pull_data.ps1` brings them to the
laptop.
