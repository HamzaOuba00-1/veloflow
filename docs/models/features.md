# Features

Every model learns from the same features (`veloflow.features.build`), built
for each example (station, origin `t`, horizon `h`) from what is known at `t`:

| Group | Features | Source |
|---|---|---|
| State | mechanical bikes, e-bikes, free docks, bikes, total, fill rate | the station at `t` |
| Recent change | change of each count over the last 15, 30 and 60 minutes | the station's 5-minute grid up to `t` |
| Neighbours | mean bikes and docks, fill rate, how many are empty or full, mean change of bikes over 15 minutes, mean distance | the 5 nearest stations at `t` |
| Time | Paris hour of `t` and of `t + h`, weekday, weekend, public holiday, school holiday | calendar of the target date |
| Weather | temperature, precipitation, wind at `t + h` | Open-Meteo 15-minute **forecast** at the station's grid point (decision D23) |

The neighbours are there because the exploration found that the next hour of
a station is announced better by its five neighbours (r = 0.163) than by its
own recent change (r = 0.098), docs/data/eda.md.

Unknown values stay null (a station seen for less than an hour, no weather
for that time); gradient boosting handles them.

## No look-ahead

States are read on the 5-minute grid with an as-of join backwards, in a
window that ends at the last origin of the batch. A test builds the features
twice, once with the whole history and once with history and examples cut at
a time `C`, and requires identical features for every origin up to `C`. On
the real data of 9 and 10 November the check passes for a cut at noon.

The first run of that check failed, without any look-ahead: the grid stopped
at the last version received, so with history cut at 12:00:00 and the last
version at 11:59:40, the origin 12:00 had no state. A forecaster running in
production would hit the same gap, since it forecasts "now", just after the
last version. `datasets.resample_status` now takes an `end`, and the features
extend the grid to the last origin.

## Cost

One test day (1.3 million examples, three horizons): 1.4 seconds and 183 MB
for the features (measured on a 3 GB Linux test machine, not the laptop).

Weather is in the silver layer for 8 and 9 November only (downloaded on 10
November); later days have null weather until
`uv run veloflow-weather-history --start 2026-11-08` is run again.
