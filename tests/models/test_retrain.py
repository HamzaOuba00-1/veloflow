"""Tests for the weekly retraining job (champion against challenger)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from veloflow.features.build import FeatureContext, make_context
from veloflow.models import retrain as job
from veloflow.models.gbm import GradientBoosting
from veloflow.models.registry import Registry

STATIONS = pl.DataFrame(
    {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
    schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
)
NOW = datetime(2026, 11, 11, 3, 15, tzinfo=UTC)


def small(context: FeatureContext) -> GradientBoosting:
    return GradientBoosting(
        context, params={"n_estimators": 10, "num_leaves": 7, "min_child_samples": 5, "verbose": -1}
    )


def run(silver_dir: Path, storage: Any) -> dict[str, Any]:
    return job.retrain(
        silver_dir,
        Registry(storage, "gs://models"),
        NOW,
        holdout_days=1,
        context=make_context(STATIONS),
        new_model=small,
    )


def test_a_first_challenger_faces_persistence_then_the_champion_stays_unless_beaten(
    silver_dir: Path, memory_storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job, "better", lambda new, old: True)
    first = run(silver_dir, memory_storage)
    assert first["decision"] == "promoted"
    assert first["against"] == "persistence"  # no champion yet
    assert first["holdout"] == ["2026-11-10"]  # the last complete Paris day
    assert {"challenger", "persistence"} <= set(first["scores"])
    assert first["scores"]["challenger"]["examples"] > 0
    pointer = json.loads(memory_storage.objects["gs://models/champion.json"])
    assert pointer["current"] == first["version"]
    assert f"gs://models/versions/{first['version']}/lightgbm.pkl" in memory_storage.objects

    # The champion was refitted on every day, 10 November included: no day is
    # left that it has not seen, so the next run has nothing to compare on.
    again = run(silver_dir, memory_storage)
    assert again["decision"] == "skipped"
    assert (
        json.loads(memory_storage.objects["gs://models/champion.json"])["current"]
        == (first["version"])
    )


def test_a_worse_challenger_is_recorded_not_promoted(
    silver_dir: Path, memory_storage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job, "better", lambda new, old: False)
    entry = run(silver_dir, memory_storage)
    pointer = json.loads(memory_storage.objects["gs://models/champion.json"])
    assert entry["decision"] == "rejected"
    assert pointer["current"] is None
    assert pointer["history"][-1]["decision"] == "rejected"
    assert f"gs://models/versions/{entry['version']}/evaluation.json" in memory_storage.objects
    assert f"gs://models/versions/{entry['version']}/lightgbm.pkl" not in memory_storage.objects


def test_scores_and_the_rule() -> None:
    frame = pl.DataFrame(
        {
            **{f"pred_{n}": [1.0, 2.0] for n in ("mechanical", "ebike", "docks")},
            **{f"y_{n}": [1, 4] for n in ("mechanical", "ebike", "docks")},
            "p_empty": [0.5, 0.0],
            "p_full": [0.0, 1.0],
            "y_empty": [True, False],
            "y_full": [False, True],
            "suspected_frozen": [False, False],
        }
    )
    result = job.scores(frame)
    assert result["mae"] == 1.0  # errors 0 and 2
    assert result["brier"] == pytest.approx(0.0625)  # (0.25 + 0 + 0 + 0) / 4
    assert job.better({"mae": 1.0, "brier": 0.1}, {"mae": 1.0, "brier": 0.2})
    assert not job.better({"mae": 0.9, "brier": 0.3}, {"mae": 1.0, "brier": 0.2})
    assert job.day_of("gs://d/silver/station_status/date=2026-11-17/station_status.parquet") == (
        date(2026, 11, 17)
    )
    assert job.day_of("gs://d/raw/station_information/station_information_2026-11-16.jsonl.gz") == (
        date(2026, 11, 16)
    )


def test_fetch_downloads_the_recent_shared_days(
    silver_dir: Path, memory_storage: Any, tmp_path: Path
) -> None:
    for path in silver_dir.rglob("*.parquet"):
        memory_storage.write(
            f"gs://data/{path.relative_to(silver_dir).as_posix()}", path.read_bytes()
        )
    days = job.fetch_data(memory_storage, "gs://data", tmp_path / "work", date(2026, 11, 9))
    assert days == [date(2026, 11, 9), date(2026, 11, 10)]
    assert (tmp_path / "work/silver/station_status/date=2026-11-10/station_status.parquet").exists()
    assert not (tmp_path / "work/silver/station_status/date=2026-11-08").exists()
