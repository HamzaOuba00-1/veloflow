"""Save and load a trained model with a description of how it was trained.

A model is saved as two files: ``<name>.pkl`` (the fitted forecaster, pickled)
and ``<name>.json`` (when, on what data, which version of the code). Pickle
runs code when loaded: only load files this project wrote.
"""

import json
import pickle
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from veloflow import __version__
from veloflow.models.gbm import GradientBoosting

MODEL_NAME = "lightgbm"


@dataclass(frozen=True, slots=True)
class ModelCard:
    """How a saved model was trained."""

    name: str
    trained_at: str
    data_start: str
    data_end: str
    examples: int
    package_version: str


def save_model(model: GradientBoosting, card: ModelCard, folder: Path) -> Path:
    """Write ``<name>.pkl`` and ``<name>.json`` atomically; return the pickle path."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{card.name}.pkl"
    partial = target.with_suffix(".pkl.part")
    with partial.open("wb") as file:
        pickle.dump(model, file, protocol=pickle.HIGHEST_PROTOCOL)
    partial.replace(target)
    target.with_suffix(".json").write_text(json.dumps(asdict(card), indent=2), encoding="utf-8")
    return target


def load_model(path: Path) -> tuple[GradientBoosting, ModelCard]:
    """Load a model written by ``save_model``.

    Raises:
        TypeError: the file does not hold a ``GradientBoosting`` model.
    """
    with path.open("rb") as file:
        model = pickle.load(file)
    if not isinstance(model, GradientBoosting):
        raise TypeError(f"{path} holds a {type(model).__name__}, not a model")
    card = ModelCard(**json.loads(path.with_suffix(".json").read_text(encoding="utf-8")))
    return model, card


def new_card(start: datetime, end: datetime, examples: int, now: datetime) -> ModelCard:
    """Describe a model trained now on data from ``start`` to ``end``."""
    return ModelCard(
        name=MODEL_NAME,
        trained_at=now.isoformat(timespec="seconds"),
        data_start=start.isoformat(timespec="seconds"),
        data_end=end.isoformat(timespec="seconds"),
        examples=examples,
        package_version=__version__,
    )
