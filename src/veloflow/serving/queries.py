"""Questions asked of a forecast document, shared by the API and the app.

Every function works on the document written by ``veloflow-predict`` (its
``stations`` list), so that the API and the app always give the same answer.
"""

import math
from datetime import datetime
from typing import Any, Literal

EARTH_RADIUS_M = 6_371_000.0
# Forecasts older than this are flagged: the VM job runs every 10 minutes.
STALE_AFTER_S = 30 * 60

Event = Literal["empty", "full"]
Need = Literal["bike", "dock"]
NEED_EVENT: dict[str, Event] = {"bike": "empty", "dock": "full"}


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def freshness(document: dict[str, Any], now: datetime) -> tuple[Literal["ok", "stale"], float]:
    """Status and age in seconds of a document."""
    age = (now - datetime.fromisoformat(document["generated_at"])).total_seconds()
    return ("stale" if age > STALE_AFTER_S else "ok"), age


def critical(
    stations: list[dict[str, Any]],
    event: Event,
    horizon: str,
    limit: int,
    min_probability: float = 0.0,
) -> list[tuple[float, dict[str, Any]]]:
    """Stations most likely to be empty (or full) at the horizon, with that probability."""
    ranked = sorted(
        ((s["forecasts"][horizon][f"p_{event}"], s) for s in stations if horizon in s["forecasts"]),
        key=lambda pair: (-pair[0], pair[1]["station_id"]),
    )
    return [pair for pair in ranked if pair[0] >= min_probability][:limit]


def options_near(
    stations: list[dict[str, Any]],
    lat: float,
    lon: float,
    need: Need,
    horizon: str,
    radius_m: float,
) -> list[dict[str, Any]]:
    """Stations within ``radius_m``, nearest first, with the chance of success.

    The chance is ``1 - P(empty)`` to find a bike and ``1 - P(full)`` to find
    a dock, at the horizon (the time it takes to get there).
    """
    event = NEED_EVENT[need]
    options = [
        {
            "station": s,
            "distance_m": round(distance_m(lat, lon, s["lat"], s["lon"]), 1),
            "probability": round(1.0 - s["forecasts"][horizon][f"p_{event}"], 3),
        }
        for s in stations
        if horizon in s["forecasts"]
    ]
    return sorted(
        (o for o in options if o["distance_m"] <= radius_m), key=lambda o: o["distance_m"]
    )


def best_option(options: list[dict[str, Any]], min_probability: float) -> dict[str, Any] | None:
    """The nearest option with at least ``min_probability``."""
    return next((o for o in options if o["probability"] >= min_probability), None)
