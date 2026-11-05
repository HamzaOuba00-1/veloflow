# Data storage layout

VéloFlow keeps two layers of `station_status` data. The raw layer is the source
of truth; everything else is derived from it and can be rebuilt.

| Layer | Where | Format | Written by |
|---|---|---|---|
| Raw (bronze) | Collector VM | `data/raw/<feed>/<feed>_YYYY-MM-DD.jsonl.gz` | `veloflow-collector` |
| Compacted (silver) | VM, then laptop | `data/silver/station_status/date=YYYY-MM-DD/station_status.parquet` | `veloflow-compact` |

## Raw layer

One gzip JSON Lines file per feed and UTC day. Each line is one feed version
that differs from the recent ones, with the response body kept verbatim:

```json
{"fetched_at": "...", "fingerprint": "sha256...", "status": "ok", "content": "{...}"}
```

`status` is `ok`, `degraded` (more than 10 % of station records rejected) or
`format_error` (unparseable response). Non-`ok` lines are evidence, not data.

## Compacted layer

One row per station per valid feed version, sorted by feed time then station:

| Column | Type | Notes |
|---|---|---|
| `feed_updated_at` | datetime (UTC) | Feed generation time: the observation time |
| `fetched_at` | datetime (UTC) | When the collector received it |
| `station_id` | int64 | Join key with `station_information` |
| `mechanical`, `ebike`, `docks` | int16 | Available bikes by type, free docks |
| `is_installed`, `is_renting`, `is_returning` | bool | Service flags |
| `last_reported` | datetime (UTC) | Staleness checks only |

The job skips non-`ok` lines, unparseable or degraded versions, and duplicate
fingerprints (a restarted collector can archive a version twice). Running it
twice on the same day produces the same file; the file is written under a
temporary name and renamed, so readers never see a partial file.

## Measurements (5 November 2026)

**Format choice, on 17 real versions (09:41–09:57 UTC):**

| Layout | Size | Extrapolated to a day |
|---|---|---|
| Raw gzip JSON | 546 KB | ~43 MB |
| Parquet zstd, full snapshots | 32 KB | ~2.5 MB |
| Parquet zstd, changed stations only | 31 KB | ~2.4 MB |
| Parquet snappy, full snapshots | 57 KB | ~4.4 MB |

Columnar compression makes full snapshots as small as change-only rows, so we
keep full snapshots: no state reconstruction is needed when reading. zstd is
about half the size of snappy for this data.

**Full-day run, on a synthetic day** (1,440 versions built from the real sample,
150 stations changing per minute, 52 MB of raw gzip):

| Measure | Value |
|---|---|
| Rows | 2,188,800 |
| Parquet file | 1.2 MB |
| Duration | 29 s |
| Peak memory | 212 MB (380 MB before sorting versions individually) |

The extrapolations above give 1 to 3 MB per day, so 30 to 90 MB per month to
ship from the VM: far below the 1 GB free monthly transfer. Real figures will be
measured on the first collected days.

## Licence

Data derived from the Vélib' Métropole feed stays under the
[ODbL](https://opendatacommons.org/licenses/odbl/1-0/).
