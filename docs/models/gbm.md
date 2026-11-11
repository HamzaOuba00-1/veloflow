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

## 80 % intervals (criterion 4)

Split conformal prediction: the regressors learn on the older 80 % of the
training origins; on the newest 20 %, `k` is the `ceil((n + 1) x 0.8)`-th
smallest absolute error of the rounded forecast, one `k` per horizon and
count. The interval is `rounded forecast ± k`, within 0 and the cap. A first
version took NumPy's quantile, whose position is `(n - 1) x p`: one rank too
far. A test with ten hand-written errors caught it.

On 9-10 November the intervals are valid but too wide: coverage between
82.7 % and 90.5 % (criterion 4 asks for 77-83 %), mean width 0.9 to 4.8.
Counts are whole numbers, so coverage moves in steps: at 15 minutes `k` is
0 or 1 (mean width 0.9 for mechanical bikes), and going from `± 0` to `± 1`
jumps past 80 % to 86-90 %. No symmetric whole-number interval lands inside
77-83 % there. Only docks at 30 minutes pass (82.7 %). Possible fixes, to weigh with more
data: separate lower and upper `k`, or one `k` per group of stations
(Mondrian conformal prediction).

## Subgroups (criterion 5)

`veloflow.evaluation.subgroups`: suburbs (station code from 21000, except the
two 600xx stations on the Seine banks in Paris; 522 of 1,522 stations), peak
hours (weekdays 07:00-10:00 and 17:00-20:00, Paris), rain (forecast of at
least 0.1 mm in the target quarter hour) and weekend. A subgroup degrades when
the model's error ratio to persistence (three counts summed) is more than 15 %
above its ratio on all examples. Comparing ratios keeps a group that is harder
for everyone, such as peak hours, from counting against the model.

On 9-10 November LightGBM passes the 9 checks: its ratio moves by at most
+2.7 % in the suburbs, +1.4 % in the rain (26,773 examples, 9 November only:
the weather of later days is not downloaded yet) and improves at peak hours
(-1 % to -7 %). There is no weekend test day yet, so that subgroup is not
checked. With criteria 1 to 5, LightGBM passes 21 of 38 checks.

## Known limits

- Training examples are built for the whole history at every test day, then
  sampled; past a few weeks this will need sampling by day before building.
- Intervals over-cover (see above).
