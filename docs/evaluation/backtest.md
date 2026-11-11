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
