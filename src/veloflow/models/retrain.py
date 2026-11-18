"""Weekly retraining: a challenger against the champion, promoted only if better.

``veloflow-retrain`` runs as a Cloud Run job, started every Monday by Cloud
Scheduler (``infra/gcp/create_training.ps1``):

1. it downloads the recent silver days and station references that the VM
   shares every night (``veloflow-share-data``), and the archived weather
   forecasts of the same days from Open-Meteo;
2. the **holdout** is the last ``--holdout-days`` complete Paris days after
   the champion's data: data neither model has seen;
3. the **challenger** learns from the ``--train-days`` days before the
   holdout, with the settings of the champion;
4. both are scored by the backtest bench on the holdout (the same examples,
   frozen stations left out), and persistence as a reference;
5. the challenger is promoted when its mean absolute error and its Brier
   score are both no worse than the champion's (persistence's when there is
   no champion yet). It is then refitted on the latest ``--train-days`` days
   and stored as a new version; ``champion.json`` points to it and keeps the
   former champion as ``previous`` (``infra/gcp/rollback_model.ps1`` goes
   back to it). Otherwise the champion stays; the decision is recorded either
   way.
"""

import argparse
import logging
import math
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from veloflow.datasets import load_station_information, load_weather_15min
from veloflow.evaluation.backtest import (
    TRAIN_DAYS,
    SilverSource,
    day_bounds,
    evaluation_days,
    quality_masks,
    run_backtest,
)
from veloflow.evaluation.examples import TARGETS
from veloflow.features.build import FeatureContext, make_context
from veloflow.gcs import Storage, join, parse_gs_url
from veloflow.models.artifact import ModelCard, load_model, new_card, save_model
from veloflow.models.base import Forecaster, TrainingData
from veloflow.models.baselines import Persistence
from veloflow.models.gbm import GradientBoosting
from veloflow.models.registry import Registry, version_name
from veloflow.weather import history

logger = logging.getLogger(__name__)

DATA_ENV = "VELOFLOW_DATA_URL"
MODELS_ENV = "VELOFLOW_MODELS_URL"
HOLDOUT_DAYS = 7
EVENTS = ("empty", "full")
# What the bucket holds, relative to the data URL (the layout of ``data/``).
STATUS_PREFIX = "silver/station_status/"
INFORMATION_PREFIX = "raw/station_information/"


class Fitted:
    """A model already trained, scored as it is: ``fit`` learns nothing."""

    def __init__(self, model: Forecaster, name: str) -> None:
        """Wrap ``model`` under ``name``."""
        self.model, self.name = model, name

    def fit(self, data: TrainingData) -> None:
        """Nothing to learn: the model is evaluated as it was trained."""

    def predict(self, history: pl.DataFrame, examples: pl.DataFrame) -> pl.DataFrame:
        """The wrapped model's forecasts."""
        return self.model.predict(history, examples)


def day_of(url: str) -> date | None:
    """The day in the name of a shared file (``date=2026-11-17`` or ``_2026-11-17.``)."""
    for part in reversed(url.replace("=", "_").replace(".", "_").replace("/", "_").split("_")):
        try:
            return date.fromisoformat(part)
        except ValueError:
            continue
    return None


def fetch_data(storage: Storage, data_url: str, folder: Path, first: date) -> list[date]:
    """Download the shared files of ``first`` onwards into ``folder``; return the status days."""
    days = []
    for prefix in (STATUS_PREFIX, INFORMATION_PREFIX):
        bucket, root = parse_gs_url(join(data_url, "x"))
        base = f"gs://{bucket}/{root[:-1]}"
        for url in storage.list(join(data_url, prefix)):
            day = day_of(url)
            if day is None or day < first:
                continue
            target = folder / url.removeprefix(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = storage.read(url)
            if data is not None:
                target.write_bytes(data)
                if prefix == STATUS_PREFIX:
                    days.append(day)
    return sorted(days)


def _mean(frame: pl.DataFrame, expression: pl.Expr) -> float:
    value = frame.select(expression.mean()).item() if frame.height else None
    return math.nan if value is None else float(value)


def scores(frame: pl.DataFrame) -> dict[str, float]:
    """Mean absolute error of the counts, Brier score of the events, interval coverage.

    Frozen stations (rule R4b) are left out, as in the headline results.
    """
    kept = frame.filter(~pl.col("suspected_frozen"))
    errors = [(pl.col(f"pred_{n}") - pl.col(f"y_{n}")).abs() for n in TARGETS]
    squares = [(pl.col(f"p_{e}") - pl.col(f"y_{e}").cast(pl.Float64)) ** 2 for e in EVENTS]
    result = {
        "examples": float(kept.height),
        "mae": _mean(kept, pl.mean_horizontal(errors)),
        "brier": _mean(kept, pl.mean_horizontal(squares)),
    }
    if "lo_mechanical" in kept.columns:
        inside = [pl.col(f"y_{n}").is_between(f"lo_{n}", f"hi_{n}") for n in TARGETS]
        result["coverage"] = _mean(kept, pl.mean_horizontal(inside))
    return result


def better(challenger: dict[str, float], reference: dict[str, float]) -> bool:
    """No worse on both: mean absolute error and Brier score."""
    return challenger["mae"] <= reference["mae"] and challenger["brier"] <= reference["brier"]


def card_end(card: ModelCard) -> datetime:
    """When the data of a model ends, in UTC."""
    end = datetime.fromisoformat(card.data_end)
    return end if end.tzinfo else end.replace(tzinfo=UTC)


def retrain(
    data_dir: Path,
    registry: Registry,
    now: datetime,
    *,
    holdout_days: int = HOLDOUT_DAYS,
    train_days: int = TRAIN_DAYS,
    context: FeatureContext | None = None,
    new_model: Callable[[FeatureContext], GradientBoosting] = GradientBoosting,
) -> dict[str, Any]:
    """Train a challenger, compare it with the champion, promote it if better.

    ``data_dir`` must hold the silver days, station references and weather
    (``context`` replaces the last two). ``new_model`` builds an untrained
    model with the settings of the champion.

    Returns:
        The decision recorded in ``champion.json``.
    """
    source = SilverSource(data_dir)
    masks = quality_masks(source)
    days = evaluation_days(masks.fetch_times)
    if context is None:
        context = make_context(load_station_information(data_dir), load_weather_15min(data_dir))

    pointer = registry.pointer()
    champion: GradientBoosting | None = None
    champion_card: ModelCard | None = None
    if pointer and pointer.get("current"):
        with tempfile.TemporaryDirectory() as folder:
            path = registry.download(pointer["current"], Path(folder))
            champion, champion_card = load_model(path)
        champion.context = context  # station reference and weather of these days
    after = card_end(champion_card) if champion_card else None
    holdout = [d for d in days if after is None or day_bounds(d)[0] > after][-holdout_days:]
    version = version_name(now)
    entry: dict[str, Any] = {"version": version, "at": now.isoformat(timespec="seconds")}
    if not holdout:
        entry |= {"decision": "skipped", "reason": "no complete day after the champion's data"}
        logger.info("%s", entry["reason"])
        return registry.decide(entry, promote=None)["history"][-1]  # type: ignore[no-any-return]

    start = day_bounds(holdout[0])[0]
    history = source.load(start - timedelta(days=train_days), start)
    challenger = new_model(context)
    challenger.fit(TrainingData(history, masks.frozen_slots, masks.frozen_stations, start))
    del history
    scored = {
        "challenger": scores(
            run_backtest(
                Fitted(challenger, "challenger"), source, days=holdout, masks=masks, train_days=1
            ).frame
        ),
        "persistence": scores(
            run_backtest(Persistence(), source, days=holdout, masks=masks, train_days=1).frame
        ),
    }
    if champion is not None:
        scored["champion"] = scores(
            run_backtest(
                Fitted(champion, "champion"), source, days=holdout, masks=masks, train_days=1
            ).frame
        )
    reference = "champion" if champion is not None else "persistence"
    promote = better(scored["challenger"], scored[reference])
    entry |= {
        "decision": "promoted" if promote else "rejected",
        "against": reference,
        "champion": pointer.get("current") if pointer else None,
        "holdout": [d.isoformat() for d in holdout],
        "scores": scored,
    }
    logger.info("challenger %s against %s: %s", scored["challenger"], reference, entry["decision"])
    if not promote:
        registry.record_evaluation(version, entry)
        return registry.decide(entry, promote=None)["history"][-1]  # type: ignore[no-any-return]

    # Promoted: the same settings, refitted on the latest days.
    end = datetime.combine(days[-1], time(), tzinfo=UTC) + timedelta(days=2)
    latest = source.load(end - timedelta(days=train_days + 2), end)
    first_seen, last_seen = latest.select(
        pl.col("fetched_at").min(), pl.col("fetched_at").max().alias("e")
    ).row(0)
    final = new_model(context)
    final.fit(
        TrainingData(
            latest, masks.frozen_slots, masks.frozen_stations, last_seen + timedelta(microseconds=1)
        )
    )
    final.compact()
    card = new_card(first_seen, last_seen, sum(final.trained_examples.values()), now)
    with tempfile.TemporaryDirectory() as folder:
        save_model(final, card, Path(folder))
        registry.upload(version, Path(folder), entry)
    return registry.decide(entry, promote=version)["history"][-1]  # type: ignore[no-any-return]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-url", default=os.environ.get(DATA_ENV), help=f"env {DATA_ENV}")
    parser.add_argument(
        "--models-url", default=os.environ.get(MODELS_ENV), help=f"env {MODELS_ENV}"
    )
    parser.add_argument("--work-dir", type=Path, default=Path(tempfile.gettempdir()) / "veloflow")
    parser.add_argument("--holdout-days", type=int, default=HOLDOUT_DAYS)
    parser.add_argument("--train-days", type=int, default=TRAIN_DAYS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, storage: Storage | None = None) -> int:
    """Fetch the shared data, then train, compare and decide."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)
    if not args.data_url or not args.models_url:
        logger.error("set --data-url and --models-url (or %s and %s)", DATA_ENV, MODELS_ENV)
        return 1
    now = datetime.now(UTC)
    first = now.date() - timedelta(days=args.train_days + args.holdout_days + 3)
    storage = storage or Storage()
    try:
        days = fetch_data(storage, args.data_url, args.work_dir, first)
        if not days:
            logger.error("no shared silver day since %s in %s", first, args.data_url)
            return 1
        logger.info("%d silver days downloaded (%s to %s)", len(days), days[0], days[-1])
        if history.main(["--data-dir", str(args.work_dir), "--start", days[0].isoformat()]) != 0:
            logger.warning("weather history incomplete: models learn without part of it")
        decision = retrain(
            args.work_dir,
            Registry(storage, args.models_url),
            now,
            holdout_days=args.holdout_days,
            train_days=args.train_days,
        )
    finally:
        storage.close()
    logger.info("decision: %s", decision.get("decision"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
