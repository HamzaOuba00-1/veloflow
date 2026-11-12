"""Tests for training, compacting, saving and loading a model."""

import pickle
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from veloflow.features.build import make_context
from veloflow.models.artifact import load_model
from veloflow.models.gbm import GradientBoosting
from veloflow.models.train import main, train

STATIONS = pl.DataFrame(
    {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
    schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
)
SMALL: dict[str, object] = {
    "n_estimators": 5,
    "num_leaves": 7,
    "min_child_samples": 5,
    "verbose": -1,
}


def test_train_saves_a_compact_model_and_its_card(silver_dir: Path) -> None:
    now = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)

    path = train(silver_dir, now, GradientBoosting(make_context(STATIONS), params=SMALL))

    model, card = load_model(path)
    assert path == silver_dir / "models" / "lightgbm.pkl"
    assert card.trained_at == "2026-11-12T01:00:00+00:00"
    assert card.data_end.startswith("2026-11-10T23:55:00")
    assert card.examples == sum(model.trained_examples.values()) > 0
    # Compact: one row per station, type of day and slot, no day.
    assert model.sums is not None
    assert model.sums["day"].null_count() == model.sums.height
    assert model.sums.select("station_id", "day_type", "slot").is_duplicated().sum() == 0


def test_loading_something_else_fails(tmp_path: Path) -> None:
    path = tmp_path / "lightgbm.pkl"
    path.write_bytes(pickle.dumps({"not": "a model"}))

    with pytest.raises(TypeError, match="dict"):
        load_model(path)


def test_compact_before_fit_fails() -> None:
    with pytest.raises(RuntimeError, match="fit"):
        GradientBoosting(make_context(STATIONS)).compact()


def test_command_without_data_fails(tmp_path: Path) -> None:
    assert main(["--data-dir", str(tmp_path)]) == 1


def test_command_trains_with_the_default_model(silver_dir: Path) -> None:
    assert main(["--data-dir", str(silver_dir)]) == 0

    model, card = load_model(silver_dir / "models" / "lightgbm.pkl")
    assert model.params["n_estimators"] == 300
    assert card.examples > 0
