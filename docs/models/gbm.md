# LightGBM model

`veloflow.models.gbm.GradientBoosting`, a global model: one set of models per
horizon (15, 30, 60 minutes), trained on every station at once.

- **Counts**: three regressors forecast the *change* of mechanical bikes,
  e-bikes and docks, with an absolute-error objective (the median change),
  since the bench scores the mean absolute error. The forecast is the state at
  the origin plus that change, between 0 and a cap (the largest total seen in
  training, or what the station holds now if that is more).
- **Events**: two classifiers forecast `empty` and `full`. They learn on the
  oldest 80 % of the training origins; isotonic regression calibrates their
  probabilities on the newest 20 %.
- **Features**: [features.md](features.md), plus the station profile (mean
  state and event frequencies on the same type of day and 15-minute slot) at
  the origin and at the target.
- **Budget**: at most 400,000 training examples per horizon, sampled with a
  fixed seed; 300 trees, 63 leaves, learning rate 0.05.

## A target leak, found by the bench

The first version computed the station profile over all training days, the
day of each training example included. With a single weekday of history, the
profile at the target slot is almost exactly the target: the model learned to
copy it. In sample, its error on mechanical bikes at 15 minutes was 0.16
against 0.30 for persistence; on the next day, 1.24. The look-ahead check
could not see it, since no forecast read the future: the leak was inside the
training set.

The fix: for a training example, the profile leaves out the example's own
day (`leave_own_day_out`), as rule R4a does for its reference. At prediction
time the test day is not in the training data, so the full profile is used.
After the fix the model sits at persistence's level on the same examples
(0.301 against 0.299).

## First results: 9 and 10 November

`uv run veloflow-backtest --data-dir data`, every silver day, R4b stations
left out:

| Horizon | Mechanical / e-bikes / docks MAE | Skill vs persistence | `empty`: Brier / ECE / AUC-PR |
|---|---|---|---|
| 15 min | 0.324 / 0.499 / 0.691 | 0.00 | 0.0211 / 0.0020 / **0.72** |
| 30 min | 0.534 / 0.787 / 1.087 | -0.01 to 0.00 | 0.0277 / 0.0046 / **0.58** |
| 60 min | 0.849 / 1.200 / 1.667 | -0.01 to +0.01 | 0.0329 / 0.0052 / **0.44** |

On counts, the model matches persistence, no better: on 9 November it was
trained on a single Sunday afternoon, on 10 November on one weekday. On 10
November alone it starts to beat persistence at 60 minutes (by 1 % on
mechanical bikes, 5 % on e-bikes and docks).

On the `empty` event it is the best model so far: the same Brier score and
calibration as calibrated persistence, and a clearly better ranking of the
stations at risk (average precision 0.72, 0.58 and 0.44 against 0.64, 0.50
and 0.38). It passes 11 of the 20 checks of the acceptance criteria, like the
profile-change baseline: criterion 1 (beat persistence by 10 to 30 %) fails,
as expected with two days of history.

Training and evaluating it on these two test days takes about 4 minutes on two
CPU cores.

## Known limits

- Training examples are built for the whole history at every test day, then
  sampled; past a few weeks this will need sampling by day before building.
- One model per horizon; quantiles and intervals come next (criterion 4).
