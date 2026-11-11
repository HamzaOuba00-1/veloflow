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
