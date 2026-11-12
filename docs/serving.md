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
  "model": {"name": "lightgbm", "trained_at": "...", "data_end": "..."},
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

## API

```powershell
$env:VELOFLOW_FORECASTS = "data/serving/forecasts.json"   # or the public URL of the bucket object
uv run veloflow-api                                         # http://localhost:8000/docs
```

`veloflow.serving.api` (FastAPI) serves the document, reloaded at most once a
minute; if a reload fails it keeps serving the previous document, and answers
503 only when it has never read one. Every request is a lookup or a sort over
about 1,500 stations.

| Endpoint | Requirement | Answer |
|---|---|---|
| `GET /health` | | origin and age of the forecasts (`stale` after 30 minutes), model in service |
| `GET /stations`, `GET /stations/{id}` | F5 | state and forecasts of every station, or one (404 if none) |
| `GET /critical?event=empty&horizon=60&limit=20` | F3 | stations most likely to be empty (or `full`), most likely first |
| `GET /recommend?lat=&lon=&need=bike&horizon=15&radius_m=500` | F2 | nearby stations by distance with the chance to find a bike (`1 - P(empty)`) or a dock (`1 - P(full)`); whether to go to the nearest; the nearest station with at least 80 % |

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
agree. Two segmented controls at the top set what one is looking for (a bike
or a dock) and when (15, 30 or 60 minutes); four tabs follow:

| Tab | Requirement | Content |
|---|---|---|
| Map | F1 | every station, coloured by the chance to find a bike or a dock (green from 80 %, orange from 50 %, red below); tapping a station opens its forecast under the map |
| Go now | F2 | from a station (Châtelet by default), within a walking distance: a verdict card (go, a better station nearby, or no safe bet), then every station in reach with its chance |
| At risk | F3 | the 20 stations most likely to be empty (or full), with their state now |
| Station | F4 | state now, then one card per horizon: counts with their 80 % interval, risks of empty and full |

### Design

A light, minimal interface in the spirit of Apple's: system fonts (no font
download), Apple system colours on a soft grey background, white cards with
large radii and soft shadows, one accent blue. Controls sit at the top as
segmented controls rather than in a sidebar, which phones hide; the four
tabs form a pill bar. Probabilities come with a thin bar and a colour, and
are shown as "> 99%" or "< 1%" at the extremes, never 100 % or 0 %: no
forecast is certain. A pill in the header says "Live" with the time of the
data, or "Delayed" with a banner when the forecasts are more than 30 minutes
old. Streamlit's own menu, toolbar and footer are hidden.

Motion is limited to a short fade-in of the cards, a hover lift on desktop
and a pulse on the live dot, and is switched off when the system asks for
reduced motion. Under 640 px wide, spacing tightens, tab icons are dropped so
the four tabs fit on one line, and the three "now" tiles stay side by side.

Cards are built by small functions returning HTML (`header_html`,
`verdict_html`, `critical_html`, `forecasts_html`...), tested without a
browser; every text from the feed is escaped. The CSS targets Streamlit's
stable test ids and ARIA attributes, not its generated class names. The
theme (colours, light mode) is passed by `veloflow-app`, which also turns off
Streamlit's usage statistics.

Streamlit is in the `app` dependency group: installed on the laptop, in CI
and in the app's container, not on the collector VM. On the document of 10
November (1,486 stations) the page renders in under a second in Streamlit's
headless test runner; the layout was checked in a headless Chrome at 1440 px
and 390 px wide.
