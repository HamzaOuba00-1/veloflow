"""Forecast every station now, from the last hour of raw collection.

``veloflow-predict`` runs where the collector runs (decision: VM + Cloud
Storage). It reads only the last ``WINDOW_MIN`` minutes of the raw archive
(the timestamp leads each line, so older lines are skipped without decoding
their JSON), the latest weather forecast snapshot, and the saved model, then
writes one JSON document with the state and forecasts of every station.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from veloflow.collector.gbfs import FeedFormatError, parse_station_status
from veloflow.collector.service import MAX_REJECTED_SHARE, STATUS_FEED
from veloflow.collector.storage import STATUS_OK, RawArchive, iter_records
from veloflow.datasets import load_station_information, resample_status
from veloflow.evaluation.examples import HORIZONS_MIN, TARGETS
from veloflow.gcs import Storage
from veloflow.models.artifact import ModelCard, load_model
from veloflow.models.base import INPUT_COLUMNS
from veloflow.models.gbm import INTERVAL_LEVEL, GradientBoosting
from veloflow.models.registry import Registry, sync_champion
from veloflow.quality.versions import select_versions, with_operational
from veloflow.serving.monitor import Monitor
from veloflow.serving.publish import publish
from veloflow.storage.compaction import STATUS_SCHEMA
from veloflow.weather.grid import GRID
from veloflow.weather.openmeteo import WeatherFormatError, parse_weather
from veloflow.weather.snapshot import FEED as WEATHER_FEED

logger = logging.getLogger(__name__)

# The longest lag feature is 60 minutes, plus the 5-minute grid tolerance.
WINDOW_MIN = 70
PREFIX = '{"fetched_at": "'
PUBLISH_ENV = "VELOFLOW_PUBLISH_URL"
MODELS_ENV = "VELOFLOW_MODELS_URL"
TRAINING_RUNS_SHOWN = 5  # decisions of the training job in the document
STEP = "5m"


def _fetched_at(line: str) -> datetime | None:
    """Fetch time of a raw archive line, read without decoding the JSON."""
    if not line.startswith(PREFIX):
        return None
    end = line.find('"', len(PREFIX))
    try:
        return datetime.fromisoformat(line[len(PREFIX) : end])
    except ValueError:
        return None


def _recent_lines(paths: list[Path], since: datetime) -> list[str]:
    import gzip

    lines: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            with gzip.open(path, "rt", encoding="utf-8") as file:
                for line in file:
                    fetched = _fetched_at(line)
                    if fetched is not None and fetched >= since:
                        lines.append(line)
        except (EOFError, OSError):
            logger.warning("%s: ends with an incomplete gzip block", path)
    return lines


@dataclass(frozen=True)
class RecentFeed:
    """What the raw archive holds for the last minutes.

    Attributes:
        status: cleaned ``station_status`` versions (rules R1, R3).
        fetches: one row per archived fetch: ``fetched_at`` and ``status``
            (``ok``, ``degraded``, ``format_error``...), for the health of the
            collector.
    """

    status: pl.DataFrame
    fetches: pl.DataFrame


def read_recent(data_dir: Path, now: datetime, minutes: int = WINDOW_MIN) -> RecentFeed:
    """The station states and the fetches of the last ``minutes`` (one read)."""
    since = now - timedelta(minutes=minutes)
    archive = RawArchive(data_dir)
    days = sorted({since.date(), now.date()})
    frames = []
    fetches: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for line in _recent_lines([archive.path_for(STATUS_FEED, day) for day in days], since):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        fetches.append((datetime.fromisoformat(record["fetched_at"]), str(record.get("status"))))
        if record.get("status") != STATUS_OK or record.get("fingerprint") in seen:
            continue
        try:
            snapshot = parse_station_status(
                record["content"].encode(), datetime.fromisoformat(record["fetched_at"])
            )
        except FeedFormatError:
            continue
        if snapshot.rejected_share > MAX_REJECTED_SHARE:
            continue
        seen.add(record["fingerprint"])
        frames.append(
            pl.DataFrame(
                [
                    (
                        snapshot.feed_updated_at,
                        snapshot.fetched_at,
                        s.station_id,
                        s.mechanical,
                        s.ebike,
                        s.docks,
                        s.is_installed,
                        s.is_renting,
                        s.is_returning,
                        s.last_reported,
                    )
                    for s in snapshot.stations
                ],
                schema=STATUS_SCHEMA,
                orient="row",
            )
        )
    raw = pl.concat(frames) if frames else pl.DataFrame(schema=STATUS_SCHEMA)
    return RecentFeed(
        status=with_operational(select_versions(raw).frame),
        fetches=pl.DataFrame(
            fetches,
            schema={"fetched_at": pl.Datetime("us", "UTC"), "status": pl.String},
            orient="row",
        ),
    )


def latest_weather(data_dir: Path, now: datetime) -> tuple[pl.DataFrame, datetime] | None:
    """15-minute rows of the most recent weather snapshot of today or yesterday, and its time."""
    archive = RawArchive(data_dir)
    for day in (now.date(), now.date() - timedelta(days=1)):
        path = archive.path_for(WEATHER_FEED, day)
        if not path.exists():
            continue
        records = [r for r in iter_records(path) if r.get("status") == STATUS_OK]
        for record in reversed(records):
            try:
                rows = parse_weather(record["content"].encode(), GRID, "minutely_15")
            except WeatherFormatError:
                continue
            return rows, datetime.fromisoformat(record["fetched_at"])
    return None


def live_examples(status: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS_MIN) -> pl.DataFrame:
    """Examples at the latest grid time, for operational stations with a known state."""
    if status.height == 0:
        return pl.DataFrame(schema={c: pl.Int64 for c in INPUT_COLUMNS})
    last, origin = status.select(
        pl.col("fetched_at").max(), pl.col("fetched_at").max().dt.truncate(STEP).alias("o")
    ).row(0)
    grid = resample_status(status, end=last).filter(
        (pl.col("time") == origin) & pl.col("operational").fill_null(value=False)
    )
    return pl.concat(
        [
            grid.select(
                "station_id",
                pl.col("time").alias("origin"),
                pl.lit(h, dtype=pl.Int16).alias("horizon_min"),
                (pl.col("time") + pl.duration(minutes=h)).alias("target_at"),
                *TARGETS,
            )
            for h in horizons
        ]
    )


def forecast_now(
    model: GradientBoosting, status: pl.DataFrame, weather: pl.DataFrame | None
) -> pl.DataFrame:
    """Forecasts of every station at the latest grid time."""
    examples = live_examples(status)
    if examples.height == 0:
        return examples
    model.context = replace(model.context, weather=weather)
    return examples.join(
        model.predict(status, examples), on=["station_id", "origin", "horizon_min"]
    )


def to_document(
    forecasts: pl.DataFrame, stations: pl.DataFrame, card: ModelCard, generated_at: datetime
) -> dict[str, Any]:
    """The JSON document served by the API: one entry per station."""
    rounded = forecasts.with_columns(
        *[pl.col(f"pred_{n}").round(1) for n in TARGETS],
        pl.col("p_empty").round(3),
        pl.col("p_full").round(3),
    )
    by_station: dict[int, dict[str, Any]] = {}
    for row in rounded.sort("station_id", "horizon_min").iter_rows(named=True):
        entry = by_station.setdefault(
            row["station_id"],
            {"now": {n: row[n] for n in TARGETS}, "forecasts": {}},
        )
        entry["forecasts"][str(row["horizon_min"])] = {
            **{n: row[f"pred_{n}"] for n in TARGETS},
            **{f"{n}_interval": [row[f"lo_{n}"], row[f"hi_{n}"]] for n in TARGETS},
            "p_empty": row["p_empty"],
            "p_full": row["p_full"],
        }
    origin = forecasts.select(pl.col("origin").max()).item() if forecasts.height else None
    return {
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "origin": origin.isoformat() if origin is not None else None,
        # The whole model card, so that the app states facts it can show, not texts.
        "model": {**asdict(card), "interval_level": INTERVAL_LEVEL},
        "stations": [
            {
                "station_id": s["station_id"],
                "code": s["station_code"],
                "name": s["name"],
                "lat": s["lat"],
                "lon": s["lon"],
                "capacity": s["capacity"],
                **by_station[s["station_id"]],
            }
            for s in stations.sort("station_id").iter_rows(named=True)
            if s["station_id"] in by_station
        ],
    }


def write_document(document: dict[str, Any], path: Path) -> bytes:
    """Write the document atomically (a reader never sees half a file); returns its bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    partial.write_bytes(payload)
    partial.replace(path)
    return payload


def champion_from(url: str, folder: Path) -> dict[str, Any] | None:
    """Bring the registry's champion into ``folder``; ``champion.json``, or None.

    The registry must never stop the forecasts: when it cannot be read, the
    model already in ``folder`` stays in service.
    """
    storage = Storage()
    try:
        return sync_champion(Registry(storage, url), folder)
    except Exception:
        logger.exception("model registry unreachable; the local model stays in service")
        return None
    finally:
        storage.close()


def training_summary(pointer: dict[str, Any]) -> dict[str, Any]:
    """The champion and the last decisions of the training job, for the Admin page."""
    runs = []
    for entry in reversed(pointer.get("history", [])[-TRAINING_RUNS_SHOWN:]):
        scores = entry.get("scores", {})
        reference = scores.get(entry.get("against", ""), {})
        challenger = scores.get("challenger", {})
        runs.append(
            {
                "version": entry.get("version"),
                "at": entry.get("at"),
                "decision": entry.get("decision"),
                "against": entry.get("against"),
                "holdout_days": len(entry.get("holdout", [])),
                "mae": [challenger.get("mae"), reference.get("mae")],
                "brier": [challenger.get("brier"), reference.get("brier")],
            }
        )
    return {"current": pointer.get("current"), "previous": pointer.get("previous"), "runs": runs}


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Forecast now and write the JSON document."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Forecast every station now.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--model", type=Path, default=Path("data/models/lightgbm.pkl"))
    parser.add_argument("--out", type=Path, default=Path("data/serving/forecasts.json"))
    parser.add_argument(
        "--publish",
        metavar="gs://BUCKET/NAME",
        default=os.environ.get(PUBLISH_ENV) or None,
        help=f"also upload the document there, as the VM's service account (env {PUBLISH_ENV})",
    )
    parser.add_argument(
        "--models-url",
        metavar="gs://BUCKET",
        default=os.environ.get(MODELS_ENV) or None,
        help=f"model registry: download a new champion before forecasting (env {MODELS_ENV})",
    )
    args = parser.parse_args(argv)
    moment = now or datetime.now(UTC)
    pointer = champion_from(args.models_url, args.model.parent) if args.models_url else None
    model, card = load_model(args.model)
    recent = read_recent(args.data_dir, moment)
    weather = latest_weather(args.data_dir, moment)
    forecasts = forecast_now(model, recent.status, weather[0] if weather else None)
    if forecasts.height == 0:
        logger.error("no recent station state in %s", args.data_dir)
        return 1
    document = to_document(forecasts, load_station_information(args.data_dir), card, moment)
    try:
        document["status"] = Monitor(args.data_dir / "serving" / "monitor").update(
            forecasts,
            recent.status,
            recent.fetches,
            card,
            INTERVAL_LEVEL,
            weather[1] if weather else None,
            moment,
        )
    except Exception:  # monitoring must never stop the forecasts from being published
        logger.exception("monitoring failed; the document is published without it")
    if pointer is not None and "status" in document:
        document["status"]["training"] = training_summary(pointer)
    payload = write_document(document, args.out)
    logger.info("%d stations forecast at %s", len(document["stations"]), document["origin"])
    if args.publish:
        sent = publish(payload, args.publish)
        logger.info("published to %s (%d KB gzipped)", args.publish, sent // 1024)
    return 0


if __name__ == "__main__":
    sys.exit(main())
