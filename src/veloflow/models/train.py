"""Train the LightGBM model on every collected day and save it.

``veloflow-train`` is the production counterpart of the backtest: the same
model, the same quality masks, trained on all the silver data at once. The
saved model goes to ``data/models/`` (``lightgbm.pkl`` and ``lightgbm.json``),
where ``veloflow-predict`` reads it.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from veloflow.datasets import load_station_information, load_station_status, load_weather_15min
from veloflow.features.build import make_context
from veloflow.models.artifact import new_card, save_model
from veloflow.models.base import TrainingData
from veloflow.models.gbm import GradientBoosting
from veloflow.quality.freeze import change_counts, frozen_feed_slots, suspected_frozen_stations

logger = logging.getLogger(__name__)

MODELS_DIR = Path("models")


def train(data_dir: Path, now: datetime, model: GradientBoosting | None = None) -> Path:
    """Fit on all silver days, keep what forecasting needs, save; return the path.

    Raises:
        ValueError: there is no silver data.
    """
    status = load_station_status(data_dir).frame
    if status.height == 0:
        raise ValueError(f"no silver station_status in {data_dir}")
    start, end = status.select(
        pl.col("fetched_at").min(), pl.col("fetched_at").max().alias("e")
    ).row(0)
    if model is None:
        context = make_context(load_station_information(data_dir), load_weather_15min(data_dir))
        model = GradientBoosting(context)
    data = TrainingData(
        status,
        frozen_feed_slots(change_counts(status)),
        suspected_frozen_stations(status),
        end + timedelta(microseconds=1),
    )
    model.fit(data)
    model.compact()
    card = new_card(start, end, sum(model.trained_examples.values()), now)
    path = save_model(model, card, data_dir / MODELS_DIR)
    logger.info("model trained on %s to %s, saved to %s", start, end, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Train on every silver day of ``--data-dir``."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Train and save the LightGBM model.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args(argv)
    try:
        train(args.data_dir, datetime.now(UTC))
    except ValueError as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
