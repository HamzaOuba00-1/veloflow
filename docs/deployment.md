# Deployment: the collector VM

The collector runs 24/7 on a Google Cloud **e2-micro** VM, inside the free
tier. Development, training and serving stay on the laptop. Everything the VM
runs is described by files in this repository.

| File | Role |
|---|---|
| `infra/gcp/create_collector_vm.ps1` | Creates the VM (run from the laptop, PowerShell) |
| `infra/vm/setup.sh` | Installs or updates the collector on the VM (idempotent) |
| `infra/vm/veloflow-collector.service` | systemd service: the collection loop |
| `infra/vm/veloflow-compact.service` | systemd one-shot job: raw → Parquet for yesterday |
| `infra/vm/veloflow-compact.timer` | Runs the job every night at 00:20 UTC |
| `infra/vm/veloflow-weather.service` | systemd one-shot job: archive the current weather forecast |
| `infra/vm/veloflow-weather.timer` | Runs it every hour at minute 7 (UTC) |

## VM settings and why

| Setting | Value | Reason |
|---|---|---|
| Machine | `e2-micro`, `us-east1-b` | Only free machine type; free regions are us-west1, us-central1, us-east1 |
| Boot disk | `pd-standard`, 30 GB | The default `pd-balanced` is billed; 30 GB is the free allowance |
| Network | Standard tier, ephemeral IP | Needed to reach the feed; no charge for the IP on the free tier |
| Identity | No service account | The collector calls no Google API |
| Protection | Deletion protection | The disk holds the only copy of the raw archive |
| Swap | 1 GB file, `vm.swappiness=10` | 1 GB of RAM, ~600 MB available after the OS |

## Layout on the VM

| Path | Content |
|---|---|
| `/opt/veloflow/app` | Git checkout of `main` and its `.venv` (runtime dependencies only) |
| `/opt/veloflow/python` | Python 3.12 installed by uv |
| `/var/lib/veloflow/raw` | Raw archive (bronze): GBFS responses from the collector, weather forecasts from the hourly job |
| `/var/lib/veloflow/silver` | Parquet files (silver), written by the nightly job |

All services run as the `veloflow` system user (no shell, no sudo), can write
only to `/var/lib/veloflow`, and have a memory cap (300 MB for the collector,
450 MB for the compaction, 150 MB for the weather snapshot).

## First deployment and updates

From the laptop, once the changes are pushed to `main`:

```powershell
gcloud compute scp infra/vm/setup.sh veloflow-collector:setup.sh
gcloud compute ssh veloflow-collector --command="sudo bash setup.sh"
```

On Windows, `gcloud compute scp` uses PuTTY's `pscp`, which does not expand
`~`: use a relative destination, which lands in the remote home directory.

The same two commands deploy a new version: the script pulls `main`
(fast-forward only), resynchronises dependencies from `uv.lock` and restarts
the collector.

## Day-to-day checks

```powershell
# Service state and the last log lines
gcloud compute ssh veloflow-collector --command="systemctl status veloflow-collector --no-pager"
gcloud compute ssh veloflow-collector --command="journalctl -u veloflow-collector -n 30 --no-pager"

# Next and last runs of the scheduled jobs
gcloud compute ssh veloflow-collector --command="systemctl list-timers 'veloflow-*' --no-pager"
gcloud compute ssh veloflow-collector --command="journalctl -u veloflow-weather -n 5 --no-pager"

# Data on disk, memory, disk space
gcloud compute ssh veloflow-collector --command="du -sh /var/lib/veloflow/*; free -m; df -h /"
```

## Getting the data on the laptop

```powershell
.\infra\sync\pull_data.ps1 -WhatIf   # list what would be downloaded
.\infra\sync\pull_data.ps1           # download it into data\
```

The script copies only the small datasets, with the same layout as
`/var/lib/veloflow`:

| Dataset | Size | Use |
|---|---|---|
| `silver/station_status` | ~1.8 MB a day | Exploration, backtests, training |
| `raw/weather_forecast` | ~110 KB a day | Train/serve gap of weather features |
| `raw/station_information` | ~250 KB a day | Station reference (the network grows: 1,520 then 1,522 stations) |

The raw `station_status` archive (~43 MB a day) stays on the VM as the backup
the silver layer can be rebuilt from. A file is downloaded only when it is
missing or its size changed, under a `.part` name first, then renamed: an
interrupted run never leaves a truncated file behind. Running it twice in a row
downloads nothing the second time.

## Monitoring

Three checks on [healthchecks.io](https://healthchecks.io) (free plan) watch the
VM from the outside and email an alert when something stops:

| Check | Pinged by | Schedule | Alert when |
|---|---|---|---|
| `veloflow-collector` | the collector, after a healthy poll (at most once a minute) | period 1 min, grace 10 min | no ping for 10 min, or 10 failed polls in a row (`/fail`) |
| `veloflow-compact` | systemd, after the nightly job, with its exit status | cron `20 0 * * *` UTC, grace 1 h | missing run or non-zero exit status |
| `veloflow-weather` | systemd, after each successful hourly snapshot | period 1 h, grace 1 h | no successful snapshot for 2 hours |

A single failed weather snapshot raises no alert on purpose: each snapshot
covers the next 24 hours, so one missing hour loses nothing the model needs.

The ping URLs are secrets. They live in `/etc/veloflow/veloflow.env` on the VM
(owner root, mode 600), read by systemd before it drops privileges, and are
never logged. Template: `infra/vm/veloflow.env.example`.

## Cost checks

- Budget alert `account-guard`: 1 USD per month on the billing account.
- 48 hours after any change to the VM: *Billing → Reports*, grouped by SKU. Every
  Compute Engine line must be offset by a free-tier credit.

## Known limits

- Port 22 is open to the internet on the default network (key-based SSH only).
  Planned hardening: SSH through IAP and Secure Boot.
- The external IP may change after a VM restart; `gcloud compute ssh` finds the
  VM by name, so nothing depends on it.
- Live forecasts only run while the laptop is on; collection never stops.
