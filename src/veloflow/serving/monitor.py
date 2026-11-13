"""Monitoring of the live service: collector, model in service, real accuracy.

Run by ``veloflow-predict`` after each forecast, on the VM. It keeps the
forecasts it just published, scores the earlier ones whose target time has
come (against the station states read for this run), and returns the
``status`` block of the forecast document, shown by the Admin page and the
API's ``/status``. Nothing here is written by hand: every figure is measured.

Files, in ``<data dir>/serving/monitor``:

- ``forecasts/<origin>.parquet``: the forecasts of one run, kept long enough
  to be scored (the longest horizon plus the read window);
- ``scores.parquet``: one row per (origin, horizon) with the sums of errors of
  the model and of persistence, kept ``KEEP_SCORES_DAYS`` days;
- ``models.jsonl``: one line per model put in service (its card).

Persistence (the state at the origin, "nothing changes") is the reference:
the model is worth serving only while it beats it.
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from veloflow.collector.config import CollectorSettings
from veloflow.datasets import resample_status
from veloflow.evaluation.examples import TARGETS
from veloflow.models.artifact import ModelCard

logger = logging.getLogger(__name__)

# Rules, shown with the figures they judge.
LIVE_WINDOW_H = 24  # real accuracy over the last 24 hours
KEEP_SCORES_DAYS = 7
KEEP_FORECASTS_MIN = 150  # longest horizon (60) + read window (70) + margin
MIN_PAIRS = 1_000  # station-forecasts needed before judging a horizon
COVERAGE_TOLERANCE = 0.05  # intervals: real coverage within level +- 5 points
HEALTH_WINDOW_MIN = 60  # collector health over the last hour
STALE_FETCH_MIN = 5  # no fetch for 5 minutes: the collector is late
STALE_WEATHER_H = 3  # weather snapshots are hourly

SCORE_SCHEMA: dict[str, Any] = {
    "origin": pl.Datetime("us", "UTC"),
    "horizon_min": pl.Int16,
    "n": pl.Int64,
    **{f"ae_model_{t}": pl.Float64 for t in TARGETS},
    **{f"ae_persistence_{t}": pl.Float64 for t in TARGETS},
    **{f"covered_{t}": pl.Int64 for t in TARGETS},
    **{f"{k}_{e}": pl.Float64 for e in ("empty", "full") for k in ("se_model", "se_persistence")},
    **{f"events_{e}": pl.Int64 for e in ("empty", "full")},
}
KEPT_COLUMNS = (
    "station_id",
    "origin",
    "horizon_min",
    "target_at",
    *TARGETS,
    *[f"{k}_{t}" for k in ("pred", "lo", "hi") for t in TARGETS],
    "p_empty",
    "p_full",
)


def expected_fetches(minutes: int) -> int:
    """Fetches the collector should make in ``minutes`` (its polling interval)."""
    interval = CollectorSettings.model_fields["poll_interval_s"].default
    return round(minutes * 60 / float(interval))


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment is not None else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


class Monitor:
    """Monitoring state kept on disk between runs of ``veloflow-predict``."""

    def __init__(self, folder: Path) -> None:
        """Use ``folder`` (created if needed)."""
        self.folder = folder
        self.forecasts_dir = folder / "forecasts"
        self.scores_path = folder / "scores.parquet"
        self.models_path = folder / "models.jsonl"

    # --- Forecasts kept for scoring -------------------------------------

    def keep(self, forecasts: pl.DataFrame) -> None:
        """Save the forecasts of this run, to score them when their time comes."""
        if forecasts.height == 0:
            return
        origin: datetime = forecasts.select(pl.col("origin").max()).item()
        self.forecasts_dir.mkdir(parents=True, exist_ok=True)
        path = self.forecasts_dir / f"{origin:%Y%m%dT%H%M}.parquet"
        partial = path.with_suffix(".part")
        forecasts.select(KEPT_COLUMNS).write_parquet(partial)
        partial.replace(path)

    def _kept(self, now: datetime) -> pl.DataFrame | None:
        """Kept forecasts, after deleting those too old to be scored."""
        if not self.forecasts_dir.exists():
            return None
        oldest = now - timedelta(minutes=KEEP_FORECASTS_MIN)
        frames = []
        for path in sorted(self.forecasts_dir.glob("*.parquet")):
            frame = pl.read_parquet(path)
            if frame.height == 0 or frame["origin"].max() < oldest:  # type: ignore[operator]
                path.unlink()
                continue
            frames.append(frame)
        return pl.concat(frames) if frames else None

    # --- Scoring --------------------------------------------------------

    def score(self, status: pl.DataFrame, now: datetime) -> pl.DataFrame:
        """Score the kept forecasts whose target time is in ``status``; returns all scores.

        A forecast is scored once: when its target time is on the 5-minute
        grid of the states read for this run, and its (origin, horizon) has
        no score yet.
        """
        scores = self._scores(now)
        kept = self._kept(now)
        if kept is None or status.height == 0:
            return scores
        actual = (
            resample_status(status)
            .filter(pl.col("operational").fill_null(value=False))
            .drop_nulls(list(TARGETS))
            .select("station_id", pl.col("time").alias("target_at"), *TARGETS)
        )
        pending = kept.join(
            scores.select("origin", "horizon_min"), on=["origin", "horizon_min"], how="anti"
        ).join(actual, on=["station_id", "target_at"], suffix="_actual")
        if pending.height == 0:
            return scores
        new = score_pairs(pending)
        scores = pl.concat([scores, new]).sort("origin", "horizon_min")
        self.folder.mkdir(parents=True, exist_ok=True)
        partial = self.scores_path.with_suffix(".part")
        scores.write_parquet(partial)
        partial.replace(self.scores_path)
        logger.info("scored %d forecasts (%d origin x horizon)", new["n"].sum(), new.height)
        return scores

    def _scores(self, now: datetime) -> pl.DataFrame:
        if not self.scores_path.exists():
            return pl.DataFrame(schema=SCORE_SCHEMA)
        oldest = now - timedelta(days=KEEP_SCORES_DAYS)
        return pl.read_parquet(self.scores_path).filter(pl.col("origin") >= oldest)

    # --- Models in service ----------------------------------------------

    def models(self, card: ModelCard, now: datetime) -> list[dict[str, Any]]:
        """Every model put in service, newest first; records ``card`` if it is new."""
        history: list[dict[str, Any]] = []
        if self.models_path.exists():
            history = [
                json.loads(line) for line in self.models_path.read_text("utf-8").splitlines()
            ]
        if not history or history[-1]["trained_at"] != card.trained_at:
            entry = {**asdict(card), "in_service_from": _iso(now)}
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.models_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(entry) + "\n")
            history.append(entry)
        return list(reversed(history))

    # --- The status block -----------------------------------------------

    def update(
        self,
        forecasts: pl.DataFrame,
        status: pl.DataFrame,
        fetches: pl.DataFrame,
        card: ModelCard,
        level: float,
        weather_at: datetime | None,
        now: datetime,
    ) -> dict[str, Any]:
        """Score, keep this run's forecasts, and return the ``status`` block."""
        scores = self.score(status, now)
        self.keep(forecasts)
        live = live_accuracy(scores, now, level)
        collector = collector_health(fetches, status, now)
        weather = {"fetched_at": _iso(weather_at)}
        return {
            "collector": collector,
            "weather": weather,
            "live": live,
            "models": self.models(card, now),
            "alerts": alerts(collector, weather_at, live, level, now),
            "rules": {
                "live_window_hours": LIVE_WINDOW_H,
                "min_pairs": MIN_PAIRS,
                "coverage_tolerance": COVERAGE_TOLERANCE,
                "stale_fetch_minutes": STALE_FETCH_MIN,
                "stale_weather_hours": STALE_WEATHER_H,
            },
        }


def score_pairs(pairs: pl.DataFrame) -> pl.DataFrame:
    """Sums of errors per (origin, horizon) of forecasts joined to what happened.

    ``pairs`` holds the kept columns plus ``<target>_actual``. Persistence
    forecasts the state at the origin; the empty (no bike) and full (no dock)
    events are scored with the Brier score.
    """
    actual = {t: pl.col(f"{t}_actual") for t in TARGETS}
    empty_now = (pl.col("mechanical") + pl.col("ebike")) == 0
    full_now = pl.col("docks") == 0
    empty = ((actual["mechanical"] + actual["ebike"]) == 0).cast(pl.Float64)
    full = (actual["docks"] == 0).cast(pl.Float64)
    return (
        pairs.group_by("origin", "horizon_min")
        .agg(
            pl.len().cast(pl.Int64).alias("n"),
            *[
                (pl.col(f"pred_{t}") - actual[t]).abs().sum().alias(f"ae_model_{t}")
                for t in TARGETS
            ],
            *[(pl.col(t) - actual[t]).abs().sum().alias(f"ae_persistence_{t}") for t in TARGETS],
            *[
                actual[t]
                .is_between(pl.col(f"lo_{t}"), pl.col(f"hi_{t}"))
                .sum()
                .alias(f"covered_{t}")
                for t in TARGETS
            ],
            ((pl.col("p_empty") - empty) ** 2).sum().alias("se_model_empty"),
            ((empty_now.cast(pl.Float64) - empty) ** 2).sum().alias("se_persistence_empty"),
            ((pl.col("p_full") - full) ** 2).sum().alias("se_model_full"),
            ((full_now.cast(pl.Float64) - full) ** 2).sum().alias("se_persistence_full"),
            empty.sum().cast(pl.Int64).alias("events_empty"),
            full.sum().cast(pl.Int64).alias("events_full"),
        )
        .cast(SCORE_SCHEMA)  # type: ignore[arg-type]
        .select(list(SCORE_SCHEMA))
    )


def live_accuracy(scores: pl.DataFrame, now: datetime, level: float) -> dict[str, Any]:
    """Real accuracy per horizon over the last ``LIVE_WINDOW_H`` hours."""
    recent = scores.filter(pl.col("origin") >= now - timedelta(hours=LIVE_WINDOW_H))
    horizons: dict[str, Any] = {}
    for row in (
        recent.group_by("horizon_min")
        .agg(pl.all().exclude("origin").sum())
        .sort("horizon_min")
        .iter_rows(named=True)
    ):
        n = row["n"]
        horizons[str(row["horizon_min"])] = {
            "pairs": n,
            "targets": {
                t: {
                    "mae": _ratio(row[f"ae_model_{t}"], n),
                    "mae_persistence": _ratio(row[f"ae_persistence_{t}"], n),
                    "coverage": _ratio(row[f"covered_{t}"], n),
                }
                for t in TARGETS
            },
            **{
                e: {
                    "brier": _ratio(row[f"se_model_{e}"], n),
                    "brier_persistence": _ratio(row[f"se_persistence_{e}"], n),
                    "rate": _ratio(row[f"events_{e}"], n),
                }
                for e in ("empty", "full")
            },
        }
    first = recent.select(pl.col("origin").min()).item() if recent.height else None
    return {"since": _iso(first), "interval_level": level, "horizons": horizons}


def collector_health(fetches: pl.DataFrame, status: pl.DataFrame, now: datetime) -> dict[str, Any]:
    """Fetches of the last hour, the latest one, and the stations in service."""
    hour = fetches.filter(pl.col("fetched_at") >= now - timedelta(minutes=HEALTH_WINDOW_MIN))
    last = fetches.select(pl.col("fetched_at").max()).item() if fetches.height else None
    latest = status.sort("fetched_at").group_by("station_id").last() if status.height else status
    return {
        "last_fetch": _iso(last),
        "fetches_last_hour": hour.height,
        "fetches_ok_last_hour": hour.filter(pl.col("status") == "ok").height,
        "expected_last_hour": expected_fetches(HEALTH_WINDOW_MIN),
        "stations": latest.height,
        "stations_operational": (
            latest.filter(pl.col("operational").fill_null(value=False)).height
            if latest.height
            else 0
        ),
    }


def alerts(
    collector: dict[str, Any],
    weather_at: datetime | None,
    live: dict[str, Any],
    level: float,
    now: datetime,
) -> list[dict[str, str]]:
    """What needs attention, by the rules above. Empty when all is well."""
    found: list[dict[str, str]] = []
    last = collector["last_fetch"]
    if last is None or now - datetime.fromisoformat(last) > timedelta(minutes=STALE_FETCH_MIN):
        found.append({"rule": "collector", "message": f"No fetch for {STALE_FETCH_MIN}+ minutes"})
    if weather_at is None or now - weather_at > timedelta(hours=STALE_WEATHER_H):
        found.append(
            {"rule": "weather", "message": f"No weather snapshot for {STALE_WEATHER_H}+ hours"}
        )
    for horizon, figures in live["horizons"].items():
        if figures["pairs"] < MIN_PAIRS:
            continue
        for target, t in figures["targets"].items():
            if (
                t["mae"] is not None
                and t["mae_persistence"] is not None
                and t["mae"] > t["mae_persistence"]
            ):
                found.append(
                    {
                        "rule": "accuracy",
                        "message": f"{target} at {horizon} min: worse than persistence",
                    }
                )
            if t["coverage"] is not None and abs(t["coverage"] - level) > COVERAGE_TOLERANCE:
                found.append(
                    {
                        "rule": "calibration",
                        "message": (
                            f"{target} at {horizon} min: intervals cover "
                            f"{t['coverage']:.0%} instead of {level:.0%}"
                        ),
                    }
                )
    return found
