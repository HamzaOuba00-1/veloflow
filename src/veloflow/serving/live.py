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
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from veloflow.collector.gbfs import FeedFormatError, parse_station_status
from veloflow.collector.service import MAX_REJECTED_SHARE, STATUS_FEED
from veloflow.collector.storage import STATUS_OK, RawArchive, iter_records
from veloflow.datasets import load_station_information, resample_status
from veloflow.evaluation.examples import HORIZONS_MIN, TARGETS
from veloflow.models.artifact import ModelCard, load_model
from veloflow.models.base import INPUT_COLUMNS
from veloflow.models.gbm import INTERVAL_LEVEL, GradientBoosting
from veloflow.quality.versions import select_versions, with_operational
from veloflow.storage.compaction import STATUS_SCHEMA
from veloflow.weather.grid import GRID
from veloflow.weather.openmeteo import WeatherFormatError, parse_weather
from veloflow.weather.snapshot import FEED as WEATHER_FEED

logger = logging.getLogger(__name__)

# The longest lag feature is 60 minutes, plus the 5-minute grid tolerance.
WINDOW_MIN = 70
PREFIX = '{"fetched_at": "'
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


def recent_status(data_dir: Path, now: datetime, minutes: int = WINDOW_MIN) -> pl.DataFrame:
    """Cleaned ``station_status`` versions of the last ``minutes`` (rules R1, R3)."""
    since = now - timedelta(minutes=minutes)
    archive = RawArchive(data_dir)
    days = sorted({since.date(), now.date()})
    frames = []
    seen: set[str] = set()
    for line in _recent_lines([archive.path_for(STATUS_FEED, day) for day in days], since):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
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
    return with_operational(select_versions(raw).frame)


def latest_forecast(data_dir: Path, now: datetime) -> pl.DataFrame | None:
    """15-minute rows of the most recent weather snapshot of today or yesterday."""
    archive = RawArchive(data_dir)
    for day in (now.date(), now.date() - timedelta(days=1)):
        path = archive.path_for(WEATHER_FEED, day)
        if not path.exists():
            continue
        records = [r for r in iter_records(path) if r.get("status") == STATUS_OK]
        for record in reversed(records):
            try:
                return parse_weather(record["content"].encode(), GRID, "minutely_15")
            except WeatherFormatError:
                continue
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


def write_document(document: dict[str, Any], path: Path) -> None:
    """Write the document atomically (a reader never sees half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")), "utf-8")
    partial.replace(path)


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    """Forecast now and write the JSON document."""
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Forecast every station now.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--model", type=Path, default=Path("data/models/lightgbm.pkl"))
    parser.add_argument("--out", type=Path, default=Path("data/serving/forecasts.json"))
    args = parser.parse_args(argv)
    moment = now or datetime.now(UTC)
    model, card = load_model(args.model)
    status = recent_status(args.data_dir, moment)
    forecasts = forecast_now(model, status, latest_forecast(args.data_dir, moment))
    if forecasts.height == 0:
        logger.error("no recent station state in %s", args.data_dir)
        return 1
    document = to_document(forecasts, load_station_information(args.data_dir), card, moment)
    write_document(document, args.out)
    logger.info("%d stations forecast at %s", len(document["stations"]), document["origin"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
