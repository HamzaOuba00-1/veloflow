"""Tests for the baselines, on a tiny history with values computed by hand.

Station 1 is seen on Monday 9 and Tuesday 10 November 2026, from 12:00 to
12:20 UTC (13:00-13:20 Paris): slot 13:00 holds 12:00-12:10, slot 13:15 holds
12:15-12:20. Mechanical bikes:

| | slot 13:00 | slot 13:15 |
|---|---|---|
| Monday | 2 | 6 |
| Tuesday | 2 | 0 (empty) |

No e-bikes; docks = 10 - mechanical, so the capacity seen is 10. The grid
keeps a state up to 5 minutes, so Monday 12:25 still holds 6 bikes: slot
13:15 has three Monday points and two Tuesday points, a mean of
(3 x 6 + 2 x 0) / 5 = 3.6 bikes and 6.4 docks, empty 2 times in 5.
"""

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from veloflow.evaluation.backtest import check_no_lookahead
from veloflow.models.base import TrainingData
from veloflow.models.baselines import (
    Persistence,
    ProfileDelta,
    StationProfile,
    calendar_keys,
)
from veloflow.quality.versions import with_operational
from veloflow.storage.compaction import STATUS_SCHEMA

WEDNESDAY = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)  # Wednesday 00:00 Paris


def training() -> TrainingData:
    rows = []
    for day, late in ((9, 6), (10, 0)):
        for minute in range(0, 25, 5):
            t = datetime(2026, 11, day, 12, minute, tzinfo=UTC)
            bikes = 2 if minute < 15 else late
            rows.append((t, t, 1, bikes, 0, 10 - bikes, True, True, True, t))
    history = with_operational(pl.DataFrame(rows, schema=STATUS_SCHEMA, orient="row"))
    return TrainingData(
        history,
        pl.DataFrame(schema={"slot_start": pl.Datetime("us", "UTC")}),
        pl.DataFrame(
            schema={
                "station_id": pl.Int64,
                "frozen_from": pl.Datetime("us", "UTC"),
                "frozen_until": pl.Datetime("us", "UTC"),
            }
        ),
        CUTOFF,
    )


def examples() -> pl.DataFrame:
    """Wednesday: (a) 2 bikes at 13:00 Paris; (b) full, 12 bikes; (c) unseen slot."""
    origins = [WEDNESDAY, WEDNESDAY + timedelta(minutes=5), WEDNESDAY + timedelta(hours=2)]
    return pl.DataFrame(
        {
            "station_id": [1, 1, 1],
            "origin": origins,
            "horizon_min": pl.Series([15, 15, 15], dtype=pl.Int16),
            "target_at": [t + timedelta(minutes=15) for t in origins],
            "mechanical": pl.Series([2, 12, 4], dtype=pl.Int16),
            "ebike": pl.Series([0, 0, 0], dtype=pl.Int16),
            "docks": pl.Series([8, 0, 6], dtype=pl.Int16),
        }
    )


def predict(model: Persistence | StationProfile | ProfileDelta) -> list[dict[str, object]]:
    model.fit(training())
    return model.predict(pl.DataFrame(), examples()).sort("origin").to_dicts()


def test_calendar_keys_use_paris_time() -> None:
    frame = pl.DataFrame({"t": [WEDNESDAY, datetime(2026, 11, 14, 22, 50, tzinfo=UTC)]})

    assert frame.select(calendar_keys(pl.col("t"))).rows() == [
        ("weekday", 13 * 60),
        ("weekend", 23 * 60 + 45),  # Saturday 23:50 Paris
    ]


def test_persistence() -> None:
    rows = predict(Persistence())

    assert [r["pred_mechanical"] for r in rows] == [2.0, 12.0, 4.0]
    assert [(r["p_empty"], r["p_full"]) for r in rows] == [(0.0, 0.0), (0.0, 1.0), (0.0, 0.0)]


def test_profile_forecasts_the_slot_of_the_target() -> None:
    model = StationProfile()
    rows = predict(model)

    # (a) and (b) target 13:15 and 13:20 Paris on a weekday.
    assert rows[0]["pred_mechanical"] == pytest.approx(3.6)
    assert rows[0]["pred_docks"] == pytest.approx(6.4)
    assert rows[0]["p_empty"] == pytest.approx(0.4)
    assert rows[0]["p_full"] == 0.0
    assert rows[1]["pred_mechanical"] == pytest.approx(3.6)
    # (c) targets 15:15 Paris, never seen: persistence.
    assert rows[2]["pred_mechanical"] == 4.0
    assert model.fallback_share == pytest.approx(1 / 3)


def test_profile_delta_moves_persistence_and_caps_it() -> None:
    model = ProfileDelta()
    rows = predict(model)

    # (a) profile goes from 2 to 3.6 bikes (docks 8 to 6.4) between the slots.
    assert (rows[0]["pred_mechanical"], rows[0]["pred_docks"]) == pytest.approx((3.6, 6.4))
    # (b) holds 12, more than the 10 seen in training: 12 + 1.6 is capped at
    # 12, never below the current state; 0 - 1.6 docks at zero.
    assert (rows[1]["pred_mechanical"], rows[1]["pred_docks"]) == (12.0, 0.0)
    # (c) no profile at either end: persistence.
    assert rows[2]["pred_mechanical"] == 4.0
    assert model.fallback_share == pytest.approx(1 / 3)


def test_profile_delta_calibrates_persistence() -> None:
    rows = predict(ProfileDelta())

    # Training, 15 minutes ahead from 2 bikes: origins 12:00-12:10 on Monday
    # (never empty) and 12:00-12:05 on Tuesday (empty), 2 in 5.
    assert rows[0]["p_empty"] == pytest.approx(0.4)
    # From 8 docks (bucket "3 or more"), never full in training.
    assert rows[0]["p_full"] == 0.0
    # (b) 12 bikes and 0 docks: buckets never seen, persistence (0 and 1).
    assert (rows[1]["p_empty"], rows[1]["p_full"]) == (0.0, 1.0)


@pytest.mark.parametrize("model", [StationProfile(), ProfileDelta()])
def test_predict_before_fit_fails(model: StationProfile | ProfileDelta) -> None:
    with pytest.raises(RuntimeError, match="fit"):
        model.predict(pl.DataFrame(), examples())


@pytest.mark.parametrize("model", [Persistence(), StationProfile(), ProfileDelta()])
def test_baselines_do_not_read_the_future(
    model: Persistence | StationProfile | ProfileDelta,
) -> None:
    data = training()
    model.fit(data)

    check_no_lookahead(model, data.history, examples(), WEDNESDAY + timedelta(hours=1))


def test_empty_batch() -> None:
    model = ProfileDelta()
    model.fit(training())

    assert model.predict(pl.DataFrame(), examples().head(0)).height == 0
    assert model.fallback_share is None
