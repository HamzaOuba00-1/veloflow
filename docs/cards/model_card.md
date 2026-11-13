# Model card: `lightgbm`

The model in service behind the app and the API. Its training facts (when,
on which days, how many examples, which version of the code) are written in
its card (`lightgbm.json`), published in every forecast document and shown on
the Admin page with the date it went into service.

## What it does

For one Vélib' station, at the moment `t` the forecast is made, and for each
horizon of 15, 30 and 60 minutes:

- the number of **mechanical bikes**, **e-bikes** and **free docks** at
  `t + h`, with an **80 % interval**;
- the probability that the station is **empty** (no bike of any kind) and
  **full** (no free dock) at `t + h`.

## How

`veloflow.models.gbm.GradientBoosting`, one set of models per horizon,
trained on every station at once:

- three LightGBM regressors with an absolute-error objective forecast the
  *change* of each count from `t` (the median change); the forecast is the
  count at `t` plus that change, between 0 and a cap (the largest total seen
  in training, or what the station holds now if that is more);
- two LightGBM classifiers for `empty` and `full`, calibrated by isotonic
  regression on the most recent 20 % of the training origins;
- intervals by split conformal prediction on the same 20 %: the rounded
  forecast plus or minus `k`, one `k` per horizon and count.

Settings: 300 trees, learning rate 0.05, 63 leaves, at least 200 examples per
leaf, row and column subsampling 0.8, seed 0
([gbm.md](../models/gbm.md)).

Features, all known at `t` ([features.md](../models/features.md)): the state
of the station and its change over 15, 30 and 60 minutes; its 5 nearest
neighbours; its usual profile at that time of day (computed without the
example's own day: [the leak it fixed](../models/gbm.md#a-target-leak-found-by-the-bench));
Paris time, weekday, public and school holidays; the Open-Meteo **forecast**
of temperature, precipitation and wind at `t + h`.

## Intended use

- Riders: "will there be a bike or a dock when I get there", up to an hour
  ahead (*Go now*, *Station*).
- Operators: which stations are about to be empty or full (*At risk*), as a
  ranking: the probabilities are calibrated, the ranking is the model's
  strength.

Not intended for: horizons beyond 60 minutes; days unlike the training days
(see limits); any decision about people (the data holds none).

## Evaluation

Rolling-origin backtest, one Paris day at a time, against persistence and
profile baselines, with an automatic look-ahead check
([protocol](../evaluation/backtest.md)). On 9-10 November 2026:

| | 15 min | 30 min | 60 min |
|---|---|---|---|
| MAE mechanical bikes, model / persistence | 0.324 / 0.323 | 0.534 / 0.529 | 0.849 / 0.840 |
| `empty` average precision, model / calibrated persistence | 0.72 / 0.64 | 0.58 / 0.50 | 0.44 / 0.38 |
| `empty` Brier score / calibration error | 0.021 / 0.002 | 0.028 / 0.005 | 0.033 / 0.005 |

- Counts: on par with persistence, not better. Criterion 1 of the design
  (10 to 30 % below persistence) **fails**.
- Intervals: valid but too wide, 82.7-90.5 % coverage for 80 % (criterion 4
  asks 77-83 %): counts are whole numbers and the step from `± 0` to `± 1`
  jumps past the target.
- Subgroups: suburbs, peak hours and rain degrade by at most 2.7 % against
  the 15 % allowed. No weekend test day yet.
- Acceptance: 21 of 38 checks.

In production, the VM scores every forecast against what happened and
reports the real accuracy of the last 24 hours, against persistence, on the
Admin page and at `/status` ([monitoring](../serving.md#monitoring)).

## Limits and risks

- **History**: trained on about a week of November data. No summer, no
  school holidays, no strike, no major event: the model has never seen them.
  Every figure above was measured on two test days.
- **Censored demand**: an empty station shows no bike, not how many riders
  wanted one. The model forecasts what the feed will show, not demand.
- **Capacity**: declared capacities are wrong for some stations (rule R2 of
  [quality](../data/quality.md)); the cap uses what was seen instead.
- **Frozen stations**: a station whose sensors stop reporting looks perfectly
  predictable. Suspected ones (rule R4b) are left out of the evaluation, not
  of the forecasts.
- **Weather** is a forecast, not an observation, at train and serve time
  alike; until the history is downloaded, later training days have none.
- **Retraining** is manual: train on the laptop, copy to the VM. The Admin
  page lists every model put in service.

## Data and licences

Vélib' Métropole availability (ODbL), Open-Meteo forecasts (CC BY 4.0, from
Météo-France models), French school calendar (Licence Ouverte 2.0). See the
[data card](data_card.md).
