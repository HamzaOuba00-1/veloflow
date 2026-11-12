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
