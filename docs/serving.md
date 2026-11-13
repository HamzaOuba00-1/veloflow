# Serving

## Architecture

The model needs the last hour of collection (recent changes, neighbours),
which only the collector VM holds; Cloud Run starts empty and keeps nothing
between starts. So the VM forecasts and Cloud Run serves:

```
VM (collector)                         Cloud Storage            Cloud Run
raw archive, last 70 min  ─┐
latest weather snapshot   ─┼─ veloflow-predict ─► forecasts.json ─► API + app
saved model (lightgbm.pkl)─┘   every 10 minutes
```

Every 10 minutes keeps the uploads under the free 5,000 writes a month of
Cloud Storage. Deployment (systemd timer, bucket, Cloud Run) is described in
[deployment.md](deployment.md) once in place.

## Training

```powershell
uv run veloflow-train --data-dir data
```

Trains the LightGBM model ([models/gbm.md](models/gbm.md)) on every silver
day, with the same quality masks as the backtest, and writes
`data/models/lightgbm.pkl` and `lightgbm.json` (when it was trained, on which
data, which version of the code). Before saving, the model keeps only the
profile totals it needs to forecast, not the per-day sums used to train
without leakage: on 5-10 November the saved model weighs 43 MB (27 MB of
trees, 15 MB of profiles). Training takes about 2 minutes on two cores.

The model is pickled: load only files written by this project.

## Forecasting now

```powershell
uv run veloflow-predict --data-dir data --model data/models/lightgbm.pkl --out data/serving/forecasts.json
```

- Reads only the last 70 minutes of the raw archive (60-minute lags plus the
  grid tolerance). The fetch time leads each archive line, so older lines are
  skipped without decoding their JSON; just after midnight, yesterday's file
  is read too. Rules R1 and R3 apply as everywhere else.
- Forecasts every operational station with a known state at the latest
  5-minute grid time, at 15, 30 and 60 minutes, with the latest Open-Meteo
  forecast snapshot as weather.
- Writes one JSON document atomically (a reader never sees half a file).

Measured with the model above and the last 70 minutes of 10 November: 0.4
seconds to forecast 1,486 stations (the others were closed or had no recent
state); the document weighs 1.0 MB, 141 KB gzipped. Memory on the VM is
measured at deployment.

## Document

```json
{
  "generated_at": "2026-11-12T08:03:30+00:00",
  "origin": "2026-11-12T08:00:00+00:00",
  "model": {"name": "lightgbm", "trained_at": "...", "data_start": "...", "data_end": "...",
            "examples": 1234567, "package_version": "...", "interval_level": 0.8},
  "stations": [
    {
      "station_id": 6245, "code": "1116", "name": "Ventadour - Opéra",
      "lat": 48.8668, "lon": 2.3344, "capacity": 27,
      "now": {"mechanical": 16, "ebike": 3, "docks": 7},
      "forecasts": {
        "15": {"mechanical": 16.0, "ebike": 3.0, "docks": 7.0,
               "mechanical_interval": [15, 17], "ebike_interval": [2, 4],
               "docks_interval": [6, 8], "p_empty": 0.0, "p_full": 0.0},
        "30": {"...": "..."},
        "60": {"...": "..."}
      }
    }
  ]
}
```

Counts are rounded to one decimal, probabilities to three; intervals are the
80 % intervals of the model.

## Monitoring

Each run of `veloflow-predict` also measures the service (`veloflow.serving.monitor`)
and adds a `status` block to the document. The VM keeps its state in
`<data dir>/serving/monitor`:

1. the forecasts it publishes are kept (`forecasts/<origin>.parquet`, about
   150 minutes: the longest horizon plus the read window);
2. at the next runs, a kept forecast whose target time is on the grid of the
   states just read is **scored once** against what happened: absolute error
   of the model and of persistence (nothing changes) per count, whether the
   count fell in the interval, squared error of the empty and full
   probabilities. Sums per (origin, horizon) go to `scores.parquet` (7 days);
3. the block reports, over the last 24 hours and per horizon: MAE of the model
   and of persistence, interval coverage, Brier scores and observed rates,
   with the number of forecasts behind each figure;
4. the collector's health comes from the archive read for the forecast
   (fetches of the last hour against one every 30 s, stations in service);
5. every model card seen is recorded with the time it went into service
   (`models.jsonl`): the history of retrainings.

Alerts follow rules written in the block itself (`rules`): no fetch for 5
minutes, no weather snapshot for 3 hours, a count forecast worse than
persistence, or intervals covering more than 5 points away from their level.
Accuracy rules wait for 1,000 forecasts per horizon. A failure of the
monitoring is logged and the document is published without the block: it
never stops the forecasts. Real accuracy is the measure that matters, but its
24-hour window covers a few days of the week at most: it complements the
backtest, it does not replace it.

## API

```powershell
$env:VELOFLOW_FORECASTS = "data/serving/forecasts.json"   # or the public URL of the bucket object
uv run veloflow-api                                         # http://localhost:8000/docs
```

`veloflow.serving.api` (FastAPI) serves the document, reloaded at most once a
minute. Every request is a lookup or a sort over about 1,500 stations.

The API and the app read the document through the same
`veloflow.serving.source.ForecastStore`:

- every document read is checked first (`check_document`): a JSON object with
  `generated_at`, `origin` (ISO dates), `model` and a non-empty list of
  stations, each with its id, name, position, current counts and at least one
  forecast. A document that fails is refused like an unreadable one;
- a failed reload (file missing or half written, bucket unreachable, broken
  document) keeps the last good document in service, so the forecasts only
  look older (`stale` after 30 minutes); the source is tried again once a
  minute, not on every request, since each try may wait for a timeout;
- with no good document at all, the API answers 503 and the app shows a
  message, never a stack trace.

In the app, a station chosen earlier that is no longer in the document (closed
or removed between two updates) is replaced by the station nearest the centre
of Paris.

| Endpoint | Requirement | Answer |
|---|---|---|
| `GET /health` | | origin and age of the forecasts (`stale` after 30 minutes), model in service |
| `GET /stations`, `GET /stations/{id}` | F5 | state and forecasts of every station, or one (404 if none) |
| `GET /critical?event=empty&horizon=60&limit=20` | F3 | stations most likely to be empty (or `full`), most likely first |
| `GET /recommend?lat=&lon=&need=bike&horizon=15&radius_m=500` | F2 | nearby stations by distance with the chance to find a bike (`1 - P(empty)`) or a dock (`1 - P(full)`); whether to go to the nearest; the nearest station with at least 80 % |
| `GET /status` | F6 | the monitoring block (see Monitoring below); 404 for a document written before it existed |

The horizon of `/recommend` is the time it takes to get there: someone 15
minutes away should read the 15-minute forecast.

On the document of 10 November 23:55 (1,486 stations), each answer takes 10
to 70 ms with the in-process test client.

## App

```powershell
$env:VELOFLOW_FORECASTS = "data/serving/forecasts.json"
uv run veloflow-app                                         # http://localhost:8501
```

`veloflow.serving.app` (Streamlit) reads the same document as the API and
asks it the same questions (`veloflow.serving.queries`), so both always
agree. A dark sidebar holds the logo, the navigation and the freshness of the
data. A station's page opens from the map (select a dot, then "Open station
page") or from the picker on the Station page.

| Page | Requirement | Content |
|---|---|---|
| Map | F1 | key figures, every station coloured by the chance to find a bike or a dock (green from 80 %, orange from 50 %, red below), the selected station's forecast beside the map (tap a station to select it), the network overview (share of stations with a bike now and expected at 15, 30, 60 minutes; bikes, e-bikes and docks available) and a tip naming the district where the risk concentrates |
| Go now | F2 | from a station, for a bike or a dock, within 5, 10 or 15 minutes' walk: every station in reach with its chance, the map with the walk to the best one, its state and forecasts, and walking directions |
| At risk | F3 | the 20 stations most likely to be empty (or full), with their district, and the same stations on the map |
| Station | F4 | state now with the change expected in 15 minutes, a map centred on the station, counts with their 80 % interval and risks of empty and full at 15, 30 or 60 minutes, the trend of each count, and a one-line reading of the risks |
| Admin | F6 | alerts, collector health (last fetch, usable fetches of the last hour, stations in service), weather snapshot, real accuracy per horizon and count against persistence with interval coverage, Brier score of the empty and full probabilities, models in service |
| About | | what the forecasts are, the data and the model, every figure read from the document (stations, horizons, training window and examples, interval level); accuracy is left to the evaluation report, so no measured figure goes stale in the app |

### No figure written by hand

Every number the app shows is computed: from the forecast document (counts,
probabilities, horizons, number of stations, the model card) or from a named
constant of `veloflow.serving.ui` (`GOOD` and `FAIR` thresholds of the colour
bands, `WATCH` for risks worth showing, `WALK_M_PER_MIN` to turn metres into
walking minutes). Legends, page subtitles and tips are written from them, so a
model with other horizons or another interval level changes the texts too. On
Go now, the chance shown is the forecast horizon nearest to the walking time
chosen. A document from an older `veloflow-predict` without the full model
card still renders: missing facts are left out, not guessed.

### Design

The layout follows the mockups of 12 November: a dark sidebar fading to a
dusk gradient, white cards on a soft lavender background, an indigo accent,
system fonts, line icons (inline SVG carried as `data:` images, since
Streamlit's HTML sanitiser drops `<svg>` elements), probabilities with a thin
bar and a colour, and "> 99%" or "< 1%" at the extremes, never 100 % or 0 %.

The logo (a cyclist, white on transparent) and the favicon (the same logo
on navy) come from the project's own design. Behind each page title and at
the bottom of the sidebar, two AI-generated pictures of the Eiffel Tower at
dusk, as an unlit silhouette, provided by Hamza Ouba. `scripts/assets/build_photos.py` crops and
compresses them from their sources (kept out of Git in `data/assets_src/`);
sources are listed in `src/veloflow/serving/assets/CREDITS.md`. Pictures are inlined
as `data:` URIs by `asset_uri`, so they are kept small (hero about 1,800 px
wide); a photo wins over the drawing of the same name (`hero.svg`,
`sidebar.svg`), which remain as a fallback.

The sidebar shows its collapse button at all times (Streamlit reveals it only
on hover), in a header row of its own; each page in the navigation, and the
selection, spans the sidebar's full width. Streamlit's header is transparent
but sits above the page: it lets clicks through, except on its buttons.

Some elements of the mockups were left out on purpose, because no data
supports them: an "overall accuracy" figure (none is measured; the map page
shows the share of stations with a bike instead), photos of the stations (no
licensed source covers 1,486 stations), a
waiting time, favourites, and a dark mode. Locating the user and searching by
address need the browser's geolocation or a geocoding service; the app
searches by station for now.

Motion is limited to a fade-in of the cards and a pulse on the live dot, and
is switched off when the system asks for reduced motion. On phones the
sidebar starts folded behind its menu button and the cards stack. The CSS
targets Streamlit's stable test ids and ARIA attributes, not its generated
class names; Streamlit's own menu and deploy button are hidden, but not the
button that opens the sidebar (a first version hid it with the toolbar,
leaving phones without navigation). `veloflow-app` passes the theme and turns
off Streamlit's usage statistics.

Figures and cards come from `veloflow.serving.ui` (pure functions, every text
from the feed escaped) and are tested without a browser; every page is also
rendered in Streamlit's headless test runner. The layout was checked in a
headless Chrome at 1600 px and 390 px wide. Streamlit is in the `app`
dependency group: installed on the laptop, in CI and in the app's container,
not on the collector VM.
