# Weather data

Weather is a model feature, so it has to follow one rule: **use only what was
known at prediction time**. When the model predicts at 14:00 the availability
of 15:00, it cannot know how much rain actually fell at 15:00, only how much
was forecast. Training on observed weather (or on a reanalysis such as ERA5)
would make offline scores look better than production ever will.

So VéloFlow uses forecasts only, from [Open-Meteo](https://open-meteo.com/):

| Endpoint | What it gives | Used for |
|---|---|---|
| Forecast API | The forecast available right now | Inference, and hourly snapshots archived on the VM |
| Historical Forecast API | Forecasts archived since 2016 | Training |

The Historical Forecast archive is built by stitching together the first hours
of successive model runs. The value it stores for 15:00 may come from a run
that was published after 14:00, so it is close to, but not exactly, what the
model would have seen. The hourly snapshots exist to measure that gap instead
of assuming it is zero.

## Measurements (9 November 2026)

Four requests over Paris for 1-7 November 2026, all with `timezone=GMT`. No
missing value in any of them.

**The 15-minute step is only real with the Météo-France model.** A quarter-hour
value is "interpolated" when it equals, within 0.05, the straight line between
the two surrounding hourly values.

| Source | Interpolated quarter-hours |
|---|---|
| `best_match` (default), archive | 432 / 501 (86 %) |
| `best_match`, live forecast | 192 / 213 (90 %) |
| `meteofrance_seamless`, archive | 48 / 501 (10 %) |

**Models disagree.** Over the same hours, `best_match` and
`meteofrance_seamless` differ by 0.76 °C on average (2.9 °C at most) and by
2.9 km/h on wind gusts (10.8 km/h at most). They do not even use the same grid
point (48.853 N vs 48.860 N).

**Time labels mark the end of the interval.** For 167 hours out of 167, hourly
`precipitation` at hour H equals the sum of the quarter-hours labelled
H-0:45, H-0:30, H-0:15 and H: it is the rain of the hour *before* H. Reading the
label as the start of the interval would shift every rain feature by one hour.
A test pins this convention
(`test_hourly_precipitation_is_the_rain_of_the_hour_before`).

**Rain is not uniform across the area.** Same week, five points:

| Point | Rain over 7 days | Rainy hours | Hours disagreeing with Paris centre on rain / no rain |
|---|---|---|---|
| Paris centre | 0.9 mm | 6 | - |
| Saint-Denis | 1.4 mm | 11 | 9 |
| Boulogne | 1.8 mm | 13 | 11 |
| Villejuif | 0.7 mm | 3 | 5 |
| Vincennes | 0.9 mm | 5 | 3 |

Temperature, by contrast, differs by only 0.2-0.4 °C on average. The week was
very dry (0.9 mm in Paris), so these figures are indicative only; they will be
recomputed on a longer period.

## Design (decision D23)

- Model `meteofrance_seamless` everywhere: real 15-minute steps, built for
  France.
- A fixed 3 x 3 grid over the station area (`veloflow.weather.grid.GRID`, about
  8 km between points). Each station takes the weather of its nearest point.
- One request covers the nine points. Open-Meteo counts each location as a call:
  hourly snapshots cost 9 x 24 = 216 calls a day, far below the free limit of
  10,000.
- Snapshots of the next 24 hours, every hour, archived as received in the
  bronze layer (`data/raw/weather_forecast/`), by `veloflow-weather-snapshot`.
  24 hours is far beyond the 60-minute horizon on purpose: if snapshots fail
  for a while, the last one still covers the hours that follow.

Variables: temperature, relative humidity, precipitation, rain, WMO weather
code, wind speed and gusts (hourly); temperature, precipitation and wind speed
(15 minutes).

## Licence

Weather data by [Open-Meteo.com](https://open-meteo.com/), under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The free API is for
non-commercial use, under 10,000 calls a day.
