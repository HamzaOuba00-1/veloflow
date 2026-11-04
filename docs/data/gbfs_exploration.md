# Vélib' GBFS feed: exploration notes

**Date:** Wednesday 4 November 2026, 16:48–16:54 (Paris time).
**Method:** `scripts/explore_gbfs.py`, one read of every feed, then 11 calls to
`station_status` spaced 30 s apart. A deeper check compared the first and last
raw snapshots.

These numbers come from a single 6-minute window on a weekday afternoon. Change
rates in particular depend on the time of day and will be measured again once
the collector runs.

## Feeds

Entry point: `https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/gbfs.json`

| Feed | Content | Announced `ttl` |
|---|---|---|
| `system_information` | Operator metadata | 3600 s |
| `station_information` | Name, coordinates, capacity | 3600 s |
| `station_status` | Bikes by type, free docks, service flags | 3600 s |

Deviations from the GBFS standard:

- No `version` field (GBFS 1.0 layout, feeds grouped by language).
- The generation time is called `lastUpdatedOther`, not `last_updated`.
- The `ttl` of 3600 s is wrong for `station_status`, which changes every minute.
  We rely on the measured refresh rate, not on `ttl`.

## Fields

`station_status`, one object per station:

| Field | Type | Notes |
|---|---|---|
| `station_id` | int | Internal id, e.g. `213688169` |
| `stationCode` | str | Public code, e.g. `"16107"` |
| `num_bikes_available` | int | Equals the sum of the per-type counts (no mismatch observed) |
| `num_bikes_available_types` | list | `[{"mechanical": 4}, {"ebike": 2}]` |
| `num_docks_available` | int | |
| `is_installed`, `is_renting`, `is_returning` | int (0/1) | Not booleans |
| `last_reported` | int | Unix seconds; see below, not a freshness signal |
| `numBikesAvailable`, `numDocksAvailable` | int | camelCase duplicates of the fields above, to be dropped |
| `station_opening_hours` | null | Always null |

`station_information` adds `name`, `lat`, `lon`, `capacity` and an optional
`rental_methods` (present for 896 stations out of 1,520, always `["CREDITCARD"]`).

`station_id` and `stationCode` map one to one: no duplicates, no mismatch
between the two feeds.

## Quality checks on one snapshot

| Check | Result |
|---|---|
| Stations in status / information | 1,520 / 1,520, same set |
| In service (`installed`, `renting`, `returning` = 1) | 1,497 |
| Installed but closed (renting = returning = 0) | 20 |
| Not installed | 3 |
| Per-type sum ≠ total bikes | 0 |
| Bikes + docks > capacity | **13**, all in service, excess from 1 to 21 |
| Capacity = 0 | 3 |
| Capacity range | 0 to 410 (two stations at 400 docks) |
| Empty (0 bikes) / full (0 docks) | 126 / 43 |

**Capacity is not a reliable upper bound.** The design document clips forecasts
between 0 and capacity. With 13 stations already above it, the bound has to be
derived from observed data (for example the maximum of `bikes + docks`), or the
station capacity re-read regularly. The cause is still unknown: stale capacity
in `station_information`, bikes parked beyond the docks, or data errors.

## `last_reported` does not track updates

Between the first and last snapshot (about 5 minutes apart), **607 stations
changed state while `last_reported` moved for none of them**. The field looks
refreshed in batches, more than ~11 minutes apart. Consequences:

- Every observation is timestamped with the feed time (`lastUpdatedOther`) and
  our own fetch time, never with `last_reported`.
- `last_reported` is only useful to spot stations frozen for hours. 18 stations
  were older than one hour; 17 of them were closed anyway. One station in
  service (`13128`) had not reported for 16 hours while showing normal values:
  this is the kind of frozen station the collector must flag. The oldest value
  dates back to February 2021.

## Refresh dynamics

- `station_status` is regenerated about every **60–61 s**.
- Every other call, the feed came back with **the same content but a timestamp
  one second older** than the previous call. The most likely explanation is
  several servers behind the same address, each publishing its own copy.
  Timestamps are therefore **not monotonic**.
- About **185 stations changed per minute** (12 % of the network).
- Response time: 0.2 s for the first calls, then a steady 3.2 s.

## Volume

| Measure | Value |
|---|---|
| One `station_status` snapshot | 454 KB raw, 30 KB gzip (ratio 15) |
| Full snapshot every minute, gzip | 42 MB/day, **1.23 GB per 30 days** |
| Changed stations only | ~266,000 rows/day at the measured rate |

Full gzip snapshots would exceed the 1 GB/month free transfer from the
collector VM. They can be kept on the VM disk (about 5 GB over the project) but
the data shipped to the laptop has to be more compact.

## Consequences for the collector

1. Poll every 30 s and deduplicate on content, not on timestamp.
2. Timestamp each observation with the feed time and the fetch time.
3. Keep raw gzip snapshots on the VM as an immutable backup.
4. Ship a compact columnar format to the laptop; size to be measured.
5. Flag stations whose `last_reported` is older than a threshold, and stations
   above their capacity, without dropping them.
6. Use a request timeout well above 3 s.

## Open questions

- Change rate by hour of the day (night vs peak hours).
- Refresh period of `last_reported`.
- Cause of the capacity overruns.
- Why responses slow down to ~3 s after the first calls.
