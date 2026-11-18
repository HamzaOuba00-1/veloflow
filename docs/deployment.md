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
| `Dockerfile`, `infra/gcp/deploy_cloud_run.ps1` | Image of the API and the app, built by Cloud Build and deployed on Cloud Run |
| `infra/gcp/enable_secure_boot.ps1` | Turns on Secure Boot on the existing VM (stops it for about two minutes) |
| `infra/gcp/create_training.ps1`, `rollback_model.ps1` | Weekly retraining: buckets, identities, Cloud Run job, schedule; going back to the previous model |
| `infra/vm/veloflow-share.*` | Shares the recent silver days with the training job every night |
| `infra/gcp/harden_project.ps1` | Removes Editor from the Compute Engine default account and closes SSH to the internet (run once from the laptop) |

## VM settings and why

| Setting | Value | Reason |
|---|---|---|
| Machine | `e2-micro`, `us-east1-b` | Only free machine type; free regions are us-west1, us-central1, us-east1 |
| Boot disk | `pd-standard`, 30 GB | The default `pd-balanced` is billed; 30 GB is the free allowance |
| Network | Standard tier, ephemeral IP | Needed to reach the feed; no charge for the IP on the free tier |
| Identity | Service account `veloflow-publisher`, scope Cloud Storage only | Its one role, `objectUser` on the forecast bucket, lets the VM replace `forecasts.json` and nothing else; no key file exists |
| Protection | Deletion protection | The disk holds the only copy of the raw archive |
| Boot | Shielded VM: Secure Boot, vTPM, integrity monitoring (`enable_secure_boot.ps1` on an existing VM) | Only a signed bootloader and kernel start; a changed boot is reported |
| SSH | Port 22 open to the IAP range `35.235.240.0/20` only (`default-allow-ssh` and `default-allow-rdp` disabled) | Nobody on the internet can reach the SSH server; access goes through Google sign-in, hence `--tunnel-through-iap` on every `gcloud compute ssh` / `scp` |
| Updates | `unattended-upgrades`, daily, no automatic reboot | Security fixes without a manual step; the collector never restarts on its own |
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
forecast: measured peak 360 MB, 25 s and 16 s of CPU for 1,496 stations). The VM installs the runtime dependencies only
(`uv sync --no-default-groups`): neither the development tools nor Streamlit.

## First deployment and updates

From the laptop, once the changes are pushed to `main`:

```powershell
gcloud compute scp --tunnel-through-iap infra/vm/setup.sh veloflow-collector:setup.sh
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="sudo bash setup.sh"
```

On Windows, `gcloud compute scp` uses PuTTY's `pscp`, which does not expand
`~`: use a relative destination, which lands in the remote home directory.

The same two commands deploy a new version: the script pulls `main`
(fast-forward only), resynchronises dependencies from `uv.lock` and restarts
the collector.

## Day-to-day checks

```powershell
# Service state and the last log lines
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="systemctl status veloflow-collector --no-pager"
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="journalctl -u veloflow-collector -n 30 --no-pager"

# Next and last runs of the scheduled jobs
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="systemctl list-timers 'veloflow-*' --no-pager"
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="journalctl -u veloflow-weather -n 5 --no-pager"

# Data on disk, memory, disk space
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="du -sh /var/lib/veloflow/*; free -m; df -h /"
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

The model in service comes from the registry, retrained every week (see
"Weekly retraining" below). Before the registry existed, or to put a model
trained on the laptop in service by hand, copy it and its card to the VM:

```powershell
gcloud compute scp --tunnel-through-iap data/models/lightgbm.pkl data/models/lightgbm.json veloflow-collector:.
gcloud compute ssh --tunnel-through-iap veloflow-collector --command="sudo install -o veloflow -g veloflow -m 644 lightgbm.pkl lightgbm.json /var/lib/veloflow/models/ && rm lightgbm.pkl lightgbm.json"
```

Until a model is there, the job is skipped (`ConditionPathExists`), not failed.

Free-tier budget: 4,320 uploads a month (class A operations, 5,000 free) and
one read a minute per running API or app instance (class B, 50,000 free);
storage is one object of about 140 KB.

## API and app on Cloud Run

One image (`Dockerfile`) serves both: the API by default, the app with the
command `veloflow-app`. It holds the code and its runtime dependencies (plus
Streamlit), no model, no data and no credentials: both services read the
public document. Built by Cloud Build from the sources, so the laptop needs no
Docker:

```powershell
.\infra\gcp\deploy_cloud_run.ps1 -WhatIf
.\infra\gcp\deploy_cloud_run.ps1
```

| Setting | API | App | Reason |
|---|---|---|---|
| Region | us-east1 | us-east1 | Same as the bucket: no inter-region traffic |
| Memory | 512 MiB | 1 GiB | Streamlit, pandas and pyarrow need more than FastAPI |
| Instances | 0 to 1 | 0 to 1 | Free tier: nothing runs without visitors; the first visit after a quiet period waits for a cold start |
| Access | public | public | The data is open; no account needed |
| Identity | `veloflow-run` | `veloflow-run` | A service account with **no role**: the services only read a public document. Without it, Cloud Run would run them as the Compute Engine default account, which Google creates with Editor on the project |
| Build identity | Compute Engine default account, `roles/run.builder` only | same image | Cloud Build builds the image as this account; Google creates it with Editor on the project, removed by `harden_project.ps1` |
| Other | | session affinity, 1 h timeout | A Streamlit session is a WebSocket held by one instance |

Artifact Registry keeps the last two images (`infra/gcp/registry_cleanup.json`):
0.5 GB is free and the image weighs a few hundred MB. CI builds the image on
every push and checks that both services start.

## Custom domain

The web app is also served at **https://veloflow.spaceh.online**, through a
Cloud Run domain mapping: free, available in us-east1, with a certificate
issued and renewed by Google. It is a Preview feature (no SLA, possibly some
extra latency from far away); a global external load balancer would do the
same for about 18 USD a month, which the free-tier rule excludes. The
`run.app` address keeps working.

| Step | Where | What |
|---|---|---|
| 1, once | Search Console | `gcloud domains verify spaceh.online`, then the TXT record it shows at the DNS host of `spaceh.online`, then *Verify*. Same Google account as gcloud. |
| 2 | Laptop | `.\infra\gcp\map_domain.ps1 -WhatIf`, then `.\infra\gcp\map_domain.ps1`: creates the mapping, prints the DNS record. |
| 3 | DNS host | `veloflow` CNAME `ghs.googlehosted.com.` (DNS only: no proxy). |
| 4 | Laptop | `.\infra\gcp\map_domain.ps1` again, until `Ready` is `True` (certificate: 15 minutes to a few hours). |

After the switch, check that a page loads and that its controls react (Streamlit talks to the server over a WebSocket).

## Weekly retraining (champion and challenger)

```text
VM, 00:50 UTC ─ veloflow-share-data ─▶ gs://veloflow-data-513808 (silver days, station references)
                                              │
Cloud Scheduler, Mondays 03:13 UTC ─▶ Cloud Run job veloflow-train (veloflow-retrain)
                                              │  challenger vs champion on the last 7 days
                                              ▼
                                 gs://veloflow-models-513808: versions/<v>/, champion.json
                                              │
VM, every 10 min ─ veloflow-predict ◀─────────┘  downloads a new champion once
```

Once, from the laptop, after `deploy_cloud_run.ps1` (the job runs the same
image):

```powershell
.\infra\gcp\create_training.ps1 -WhatIf
.\infra\gcp\create_training.ps1
```

It creates the two private buckets, the job's identity (`veloflow-trainer`:
reads the data bucket, writes the registry), the schedule's identity
(`veloflow-scheduler`: may start the job, nothing else), gives the VM's
account write access to the data bucket and read access to the registry,
deploys the job (2 vCPU, 8 GiB, one hour at most, no retry), schedules it, and
registers the model now in `data/models` as the first champion. Then add
`VELOFLOW_DATA_URL` and `VELOFLOW_MODELS_URL` to `/etc/veloflow/veloflow.env`
(template: `infra/vm/veloflow.env.example`) and run `setup.sh` again for the
new timer.

Each run (`src/veloflow/models/retrain.py`):

1. downloads the shared days of the last five weeks and their archived
   weather forecasts;
2. takes as **holdout** the last seven complete Paris days after the
   champion's data, data that no model has seen;
3. trains a **challenger** on the 28 days before the holdout, with the
   champion's settings;
4. scores the challenger, the champion and persistence with the backtest
   bench on the holdout (same examples, frozen stations left out);
5. **promotes** the challenger only if its mean absolute error and its Brier
   score are both no worse than the champion's (persistence's, without a
   champion), after refitting it on the latest 28 days. Otherwise the
   champion stays. Every decision, with its scores, goes into
   `champion.json` and appears on the Admin page.

`veloflow-predict` reads `champion.json` before each run and downloads a new
version once (card first, model last, then a `VERSION` file). If the registry
cannot be read, the local model stays in service: the forecasts never stop
for it.

Run the job now, and go back to the previous model if needed (the VM follows
within 10 minutes):

```powershell
gcloud run jobs execute veloflow-train --region=us-east1 --wait
gcloud run jobs executions list --job=veloflow-train --region=us-east1
.\infra\gcp\rollback_model.ps1 -WhatIf
.\infra\gcp\rollback_model.ps1
```

Free-tier budget: about 20 minutes of a 2 vCPU job a week (Cloud Run: 180,000
vCPU-seconds free a month), 1 of the 3 free Cloud Scheduler jobs, about 25
uploads a week to the data bucket, which deletes objects after 60 days.

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

Do not run `veloflow-compact --all` on the laptop after a pull: the VM
compacts every night, and the laptop only holds partial raw files of a few
days, so the compaction would rebuild those days from them and replace
complete silver files (it happened on 18 November; a second pull restores
them, since their size no longer matches the VM's).

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

- Kernel updates are installed automatically but wait for a manual reboot:
  `gcloud compute ssh --tunnel-through-iap veloflow-collector --command="cat /var/run/reboot-required 2>/dev/null || echo none"`.
- The external IP may change after a VM restart; `gcloud compute ssh` finds the
  VM by name, so nothing depends on it.
- The model is copied by hand after each training; the API's `/health` shows
  which model is in service.
