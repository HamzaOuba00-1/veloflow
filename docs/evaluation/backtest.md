# Backtest protocol

Every model, from the simplest baseline to the final one, is evaluated by the
same bench (`veloflow.evaluation`), on the same examples, with the same rules.
This page grows with the bench.

## Evaluation examples

An example is a station, an **origin** `t` (the moment the forecast is made,
every 5 minutes) and a **horizon** `h` (15, 30 or 60 minutes). It holds:

- the state known at `t`: mechanical bikes, e-bikes, free docks;
- the targets, observed at `t + h`: the same three counts, plus two events,
  `empty` (no bike of any type) and `full` (no free dock).

Both ends are read from the same 5-minute grid: at each grid time, the last
version fetched at or before that time, if it is at most 5 minutes old
(`datasets.resample_status`). The state at the origin is therefore exactly
what a forecaster running at `t` could have read.

An example is kept only if both its origin and its target are trustworthy
(docs/data/quality.md): the state is known, the station is operational (R3),
and the feed is neither frozen nor catching up (R4a). Suspected frozen
stations (R4b) are kept but flagged: headline results leave them out, and are
also reported with them. On a frozen station persistence is always right,
which would flatter the reference every model has to beat.

Measured on 8-10 November (58 hours, `veloflow.evaluation.examples`):

| | Examples | Share of candidates |
|---|---|---|
| Candidates (target on the grid) | 3,177,936 | 100 % |
| Origin unknown | 1,770 | 0.06 % |
| Origin not operational | 61,092 | 1.92 % |
| Origin in a frozen feed | 134,730 | 4.24 % |
| Target unknown | 0 | 0.00 % |
| Target not operational | 462 | 0.01 % |
| Target in a frozen feed | 31,437 | 0.99 % |
| **Kept** | **2,948,445** | **92.78 %** |
| of which suspected frozen (R4b) | 2,889 | 0.09 % |

The frozen feed weighs a lot here because the period is short: the single
incident of 9 November (2.5 hours) is 4 % of these 58 hours.

Among kept examples, a station is empty at the target 4.6 % of the time and
full 2.6 % of the time. Persistence ("nothing changes") gives a mean absolute
error on bikes of 0.71 at 15 minutes, 1.11 at 30 and 1.70 at 60, close to the
exploration (docs/data/eda.md) and slightly higher, since the frozen periods,
where persistence is right for free, are now excluded.

Building the examples of these 58 hours takes 6 seconds and 1.1 GB of
memory. Six weeks will not fit at once: the bench will build them one test
day at a time.

## The bench

### Forecaster interface

Every model implements `veloflow.models.base.Forecaster`:

- `fit(data)`: `data` is a `TrainingData` holding the cleaned history fetched
  strictly before the cutoff, and the quality masks. `data.examples()` builds
  the training examples on demand; all their targets fall before the cutoff,
  and suspected frozen stations are left out;
- `predict(history, examples)`: `examples` carries only what a forecaster may
  know (station, origin, horizon, target time, state at the origin), never
  the targets or the quality flags. The model returns the three counts and
  the probabilities `p_empty` and `p_full` for each example.

The bench refuses predictions with missing columns, missing or duplicated
examples, null values or probabilities outside [0, 1].

### Rolling origin, one Paris day at a time

The test days are the complete Paris days with data before them. For each
test day `D`, the model is fitted on everything fetched before midnight `D`
(Paris time), then forecasts every example whose origin falls in `D`. The
training window grows day after day. The examples are built one test day at
a time, so memory stays flat as the history grows.

The quality masks are computed once on all the data. This is the one place
where later data is used: a mask only decides which rows are trusted, it
never gives a value to a model. Computing them fold by fold would leave the
9 November incident unmasked in training, since its reference needs other
working days.

### No look-ahead, checked automatically

On the first test day, the bench runs `predict` twice: once with the whole
day, once with the history and the examples cut at noon. Forecasts made at or
before noon must be identical, or the run stops with `LookaheadError`. A test
model that reads the last state of the history is caught this way. The check
needs no knowledge of the model's internals, so it applies unchanged to every
future model.

### Metrics

`veloflow.evaluation.metrics`, by horizon and optionally by Paris hour of the
origin and type of day:

- counts: MAE and RMSE, next to the MAE of persistence on the same examples;
- events `empty` and `full`: observed rate, Brier score, log loss
  (probabilities clipped to [1e-6, 1 - 1e-6]), expected calibration error
  (10 equal-width bins) and average precision (area under the
  precision-recall curve, with tied probabilities as one threshold, as in
  scikit-learn).

Days are pooled, so each day weighs by its number of examples.

On 8-10 November the test days are 9 and 10 November (8 November is partial
and only used for training). A full run with persistence takes 6.5 seconds
and 1.2 GB of memory and passes the look-ahead check. Its results come with
the baselines.

## Baselines

`veloflow.models.baselines`, the references of the acceptance criteria
(design, section 4.5):

- **persistence**: nothing changes; event probabilities are 0 or 1;
- **profile**: the mean of the station on the same type of day (weekday or
  weekend) and Paris 15-minute slot as the target, over the training days.
  Its event probabilities are the historical frequencies of that slot, the
  reference of criterion 3;
- **persistence + profile change**: the state at the origin, moved by the
  change the profile expects between the origin slot and the target slot,
  between 0 and a cap. The cap is the largest total seen in training, or what
  the station holds now if that is more: some stations hold more than ever
  seen before (rule R2), and a first version that capped them at the training
  maximum pushed forecasts below the current state. Event probabilities are
  persistence calibrated on the training examples: the observed frequency of
  `empty` at the target, by horizon and number of bikes at the origin (0, 1,
  2, 3 or more); the same for `full` with docks.

Where a slot was never seen in training, the profiles fall back on
persistence.

### First run: 9 and 10 November

These numbers say more about the amount of data than about the baselines.
On 9 November the only training data is the afternoon of Sunday 8, so there
is no weekday profile at all: both profile baselines fall back on persistence
for every example. On 10 November the profile comes from a single weekday,
Monday 9 (10 % of its examples still fall back, mostly slots masked by the
frozen feed of 9 November). A one-day "profile" is just what that station
happened to hold the day before.

Mean absolute error on 10 November, the only day with a profile
(R4b stations left out):

| Horizon | Persistence, mechanical / docks | Persistence + profile change | Profile |
|---|---|---|---|
| 15 min | **0.30** / **0.67** | 0.48 / 0.92 | 3.22 / 4.76 |
| 30 min | **0.50** / **1.05** | 0.75 / 1.39 | 3.25 / 4.80 |
| 60 min | **0.79** / **1.63** | 1.13 / 2.02 | 3.29 / 4.87 |

With one day of history, persistence is by far the best count forecast, and
adding the profile's expected change makes it worse. The profile baselines
will be meaningful after a few weeks of weekdays and weekends.

The event probabilities tell another story. Calibrated persistence needs
little data: it pools every station into five classes per horizon. On both
days (`empty`, R4b stations left out):

| Horizon | Persistence: Brier / ECE / AUC-PR | Calibrated persistence | Profile |
|---|---|---|---|
| 15 min | 0.026 / 0.026 / 0.55 | **0.021** / **0.002** / **0.64** | 0.047 / 0.048 / 0.31 |
| 30 min | 0.037 / 0.037 / 0.40 | **0.028** / **0.003** / **0.50** | 0.053 / 0.054 / 0.25 |
| 60 min | 0.047 / 0.047 / 0.28 | **0.033** / **0.005** / **0.38** | 0.059 / 0.060 / 0.19 |

Calibration alone lowers the Brier score of persistence by 19 % at 15
minutes, 25 % at 30 and 29 % at 60, and brings its calibration error from
0.026-0.047 to below 0.005. For a 0-or-1 forecast, ECE equals the Brier score,
as the table shows.

Leaving the R4b stations in changes the mean absolute error of persistence by
less than 0.002: 7 suspected stations weigh 0.1 % of the examples.

The three baselines run in 25 seconds and 1.6 GB on these two test days.

## Report and acceptance criteria

```powershell
uv run veloflow-backtest --data-dir data                 # every silver day
uv run veloflow-backtest --data-dir data --start 2026-11-08
```

The command runs every model of `report.baselines()` on the same test days
and writes `data/reports/backtest/backtest_<start>_<end>.md` (examples per
day, errors by horizon with skill against persistence, events, error by
Paris hour, acceptance) and the metrics as Parquet. It takes 30 seconds and
2.2 GB on 9-10 November.

A test day must have at least 90 % of its 5-minute slots covered and data
before it. A first version only checked that data existed before and after a
day: with the trial file of 5 November, the empty days of 6 and 7 November and
the partial 8 November became test days. The report made it visible at once.

`veloflow.evaluation.acceptance` checks criteria 1 to 5 of the design
(section 4.5): criteria 4 (intervals) and 5 (subgroups) since the LightGBM
model, see [models/gbm.md](../models/gbm.md); criteria 6 and 7 need a
cold-start protocol and a simulator that do not exist yet. The candidate here is persistence + profile change, the only model that
is not itself a reference. On 9-10 November, with every silver day (the
17-minute trial of 5 November included, which adds a Thursday-morning profile
for two slots, so profile numbers differ slightly from the tables above), it
passes 11 checks of 20:

| Criterion | Result |
|---|---|
| 1. MAE at least 10/20/30 % below persistence | **fails** at every horizon: 13 % to 33 % *worse* |
| 2. MAE below the station profile | passes at every horizon |
| 3. Brier skill ≥ 0.2 against historical frequency, ECE ≤ 0.03 (empty, 30 min) | passes: 0.48 and 0.003 |

Read with the amount of data in mind: criteria 2 and 3 are passed against a
profile learned from at most one weekday, a weak reference. Criterion 1 is
the honest summary: nothing beats persistence on counts yet. The bench, the
references and the criteria are now fixed; the same command will be rerun as
the history grows and when real models arrive.
