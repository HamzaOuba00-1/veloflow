# Deployment: the collector VM

The collector runs 24/7 on a Google Cloud **e2-micro** VM, inside the free
tier. Every 10 minutes the VM also forecasts every station and publishes the
document to a public Cloud Storage object, read by the API and the app.
Development and training stay on the laptop. Everything the VM runs is
described by files in this repository.

| File | Role |
|---|---|
| `infra/gcp/create_collector_vm.ps1` | Creates the VM (run from the laptop, PowerShell) |
| `infra/vm/setup.sh` | Installs or updates the collector on the VM (idempotent) |
| `infra/vm/veloflow-collector.service` | systemd service: the collection loop |
| `infra/vm/veloflow-compact.service` | systemd one-shot job: raw → Parquet for yesterday, then its coverage report |
| `infra/vm/veloflow-compact.timer` | Runs the job every night at 00:20 UTC |
| `infra/vm/veloflow-weather.service` | systemd one-shot job: archive the current weather forecast |
| `infra/vm/veloflow-weather.timer` | Runs it every hour at minute 7 (UTC) |
| `infra/vm/veloflow-predict.service` | systemd one-shot job: forecast now, publish `forecasts.json` |
| `infra/vm/veloflow-predict.timer` | Runs it every 10 minutes (minutes 2, 12, 22…) |
| `infra/gcp/create_forecast_bucket.ps1` | Creates the public bucket and the VM's service account (run from the laptop) |

## VM settings and why

| Setting | Value | Reason |
|---|---|---|
| Machine | `e2-micro`, `us-east1-b` | Only free machine type; free regions are us-west1, us-central1, us-east1 |
| Boot disk | `pd-standard`, 30 GB | The default `pd-balanced` is billed; 30 GB is the free allowance |
| Network | Standard tier, ephemeral IP | Needed to reach the feed; no charge for the IP on the free tier |
| Identity | Service account `veloflow-publisher`, scope Cloud Storage only | Its one role, `objectUser` on the forecast bucket, lets the VM replace `forecasts.json` and nothing else; no key file exists |
| Protection | Deletion protection | The disk holds the only copy of the raw archive |
| Swap | 1 GB file, `vm.swappiness=10` | 1 GB of RAM, ~600 MB available after the OS |

## Layout on the VM

| Path | Content |
|---|---|
| `/opt/veloflow/app` | Git checkout of `main` and its `.venv` (runtime dependencies only) |
| `/opt/veloflow/python` | Python 3.12 installed by uv |
| `/var/lib/veloflow/raw` | Raw archive (bronze): GBFS responses from the collector, weather forecasts from the hourly job |
| `/var/lib/veloflow/silver` | Parquet files (silver), written by the nightly job |
| `/var/lib/veloflow/quality` | Daily coverage reports (JSON), written by the nightly job |
| `/var/lib/veloflow/models` | The model in service (`lightgbm.pkl` and its card `lightgbm.json`), copied from the laptop |
| `/var/lib/veloflow/serving` | The last forecast document, before its upload |

All services run as the `veloflow` system user (no shell, no sudo), can write
only to `/var/lib/veloflow`, and have a memory cap (300 MB for the collector,
450 MB for the compaction, 150 MB for the weather snapshot, 450 MB for the
forecast until measured). The VM installs the runtime dependencies only
(`uv sync --no-default-groups`): neither the development tools nor Streamlit.

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

## Live forecasts

```text
collector ─▶ raw archive ─┐
weather job ─▶ forecast ──┼─▶ veloflow-predict (every 10 min) ─▶ gs://veloflow-forecasts-513808/forecasts.json
lightgbm.pkl (laptop) ────┘                                         │ public URL
                                                                    ▼
                                                     API and app (Cloud Run), reload once a minute
```

Once, from the laptop (prints the commands first with `-WhatIf`):

```powershell
.\infra\gcp\create_forecast_bucket.ps1 -WhatIf
.\infra\gcp\create_forecast_bucket.ps1
```

It creates the bucket (us-east1, Standard class, soft delete off), lets anyone
read its objects (not list them), creates the service account, gives it
`objectUser` on this bucket only, and attaches it to the VM with the Cloud
Storage scope. Attaching requires stopping the VM: collection pauses for about
a minute.

`veloflow-predict --publish gs://…` (or `VELOFLOW_PUBLISH_URL` in the env file)
uploads the document after writing it: two HTTP requests, a token from the
metadata server, then a multipart upload. The object is stored gzipped (about
140 KB instead of 1 MB) with `Cache-Control: no-cache`, so readers never get a
copy cached by Google's edge for an hour. An upload replaces the object at
once: readers see the old document or the new one, never half of one.

After each training on the laptop, copy the model and its card (when, on what
data, which version of the code; read with the model) to the VM:

```powershell
gcloud compute scp data/models/lightgbm.pkl data/models/lightgbm.json veloflow-collector:.
gcloud compute ssh veloflow-collector --command="sudo install -o veloflow -g veloflow -m 644 lightgbm.pkl lightgbm.json /var/lib/veloflow/models/ && rm lightgbm.pkl lightgbm.json"
```

Until a model is there, the job is skipped (`ConditionPathExists`), not failed.

Free-tier budget: 4,320 uploads a month (class A operations, 5,000 free) and
one read a minute per running API or app instance (class B, 50,000 free);
storage is one object of about 140 KB.

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
| `quality/coverage` | ~1 KB a day | Daily coverage report ([data quality](data/quality.md), rule R6) |

The raw `station_status` archive (~43 MB a day) stays on the VM as the backup
the silver layer can be rebuilt from. A file is downloaded only when it is
missing or its size changed, under a `.part` name first, then renamed: an
interrupted run never leaves a truncated file behind. Running it twice in a row
downloads nothing the second time.

## Monitoring

Four checks on [healthchecks.io](https://healthchecks.io) (free plan) watch the
VM from the outside and email an alert when something stops:

| Check | Pinged by | Schedule | Alert when |
|---|---|---|---|
| `veloflow-collector` | the collector, after a healthy poll (at most once a minute) | period 1 min, grace 10 min | no ping for 10 min, or 10 failed polls in a row (`/fail`) |
| `veloflow-compact` | systemd, after the nightly job, with its exit status | cron `20 0 * * *` UTC, grace 1 h | missing run or non-zero exit status |
| `veloflow-weather` | systemd, after each successful hourly snapshot | period 1 h, grace 1 h | no successful snapshot for 2 hours |
| `veloflow-predict` | systemd, after each successful forecast and upload | period 10 min, grace 20 min | no document published for 30 minutes |

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
- The model is copied by hand after each training; the API's `/health` shows
  which model is in service.
