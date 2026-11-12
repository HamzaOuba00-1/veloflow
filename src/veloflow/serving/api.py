"""Public API: station forecasts, critical stations, "should I go now?".

The API serves the document written by ``veloflow-predict`` (a local file or
an ``https://`` URL, such as the public object in Cloud Storage), reloaded at
most once per ``ttl`` seconds. It computes nothing heavy: every request is a
lookup or a sort over about 1,500 stations.

Endpoints (OpenAPI documentation at ``/docs``):

* ``GET /health``: age of the forecasts and model in service;
* ``GET /stations`` and ``GET /stations/{station_id}`` (requirement F5);
* ``GET /critical``: stations most likely to be empty or full (F3);
* ``GET /recommend``: near a point, where to find a bike or a dock (F2).
"""

import json
import math
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

SOURCE_ENV = "VELOFLOW_FORECASTS"
DEFAULT_SOURCE = "data/serving/forecasts.json"
TTL_S = 60.0
# Forecasts older than this are flagged: the VM job runs every 10 minutes.
STALE_AFTER_S = 30 * 60
EARTH_RADIUS_M = 6_371_000.0
Horizon = Literal["15", "30", "60"]


class Counts(BaseModel):
    """Bikes and docks at a station."""

    mechanical: float
    ebike: float
    docks: float


class Forecast(BaseModel):
    """Forecast of a station at one horizon."""

    mechanical: float
    ebike: float
    docks: float
    mechanical_interval: tuple[float, float]
    ebike_interval: tuple[float, float]
    docks_interval: tuple[float, float]
    p_empty: float
    p_full: float


class Station(BaseModel):
    """A station, its state at the forecast origin and its forecasts."""

    station_id: int
    code: str | None
    name: str
    lat: float
    lon: float
    capacity: int | None
    now: Counts
    forecasts: dict[str, Forecast]


class Health(BaseModel):
    """Freshness of the forecasts in service."""

    status: Literal["ok", "stale"]
    origin: str | None
    generated_at: str
    age_seconds: float
    stations: int
    model: dict[str, str]


class Critical(BaseModel):
    """A station at risk, with its probability."""

    station_id: int
    name: str
    lat: float
    lon: float
    probability: float
    now: Counts


class Option(BaseModel):
    """A nearby station and the chance to find what is needed there."""

    station_id: int
    name: str
    distance_m: float
    probability: float
    now: Counts


class Recommendation(BaseModel):
    """Answer to "should I go now?" near a point."""

    need: Literal["bike", "dock"]
    horizon: Horizon
    nearest: Option | None
    go_to_nearest: bool
    best: Option | None
    options: list[Option]


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def read_source(source: str) -> dict[str, Any]:
    """The forecast document at a local path or an ``http(s)`` URL."""
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, timeout=10.0)
        response.raise_for_status()
        loaded: dict[str, Any] = response.json()
        return loaded
    document: dict[str, Any] = json.loads(Path(source).read_text(encoding="utf-8"))
    return document


class ForecastStore:
    """The current document, reloaded at most once per ``ttl`` seconds."""

    def __init__(
        self,
        source: str,
        ttl: float = TTL_S,
        reader: Callable[[str], dict[str, Any]] = read_source,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Remember where to read; nothing is read before the first request."""
        self.source, self.ttl, self.reader, self.clock = source, ttl, reader, clock
        self._document: dict[str, Any] | None = None
        self._loaded_at = -math.inf

    def document(self) -> dict[str, Any]:
        """The cached document, refreshed when older than ``ttl``.

        A failed refresh keeps serving the previous document; with none, the
        error becomes a 503.
        """
        if self.clock() - self._loaded_at >= self.ttl:
            try:
                self._document = self.reader(self.source)
                self._loaded_at = self.clock()
            except (OSError, ValueError, httpx.HTTPError) as error:
                if self._document is None:
                    raise HTTPException(503, f"forecasts unavailable: {error}") from error
        assert self._document is not None
        return self._document

    def stations(self) -> list[dict[str, Any]]:
        """Stations of the current document."""
        stations: list[dict[str, Any]] = self.document()["stations"]
        return stations


def create_app(store: ForecastStore) -> FastAPI:
    """The FastAPI application over a forecast store."""
    app = FastAPI(
        title="VéloFlow",
        version="0.1.0",
        description="Forecasts of Vélib' Métropole bikes and docks at 15, 30 and 60 minutes.",
    )

    @app.get("/health")
    def health() -> Health:
        """Age of the forecasts in service; ``stale`` after 30 minutes."""
        document = store.document()
        generated = datetime.fromisoformat(document["generated_at"])
        age = (datetime.now(UTC) - generated).total_seconds()
        return Health(
            status="stale" if age > STALE_AFTER_S else "ok",
            origin=document["origin"],
            generated_at=document["generated_at"],
            age_seconds=round(age, 1),
            stations=len(document["stations"]),
            model={k: str(v) for k, v in document["model"].items()},
        )

    @app.get("/stations")
    def stations() -> list[Station]:
        """Every station with its state and forecasts."""
        return [Station(**s) for s in store.stations()]

    @app.get("/stations/{station_id}")
    def station(station_id: int) -> Station:
        """One station; 404 if it has no forecast."""
        for entry in store.stations():
            if entry["station_id"] == station_id:
                return Station(**entry)
        raise HTTPException(404, f"no forecast for station {station_id}")

    @app.get("/critical")
    def critical(
        event: Literal["empty", "full"] = "empty",
        horizon: Horizon = "60",
        limit: Annotated[int, Query(ge=1, le=500)] = 20,
        min_probability: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
    ) -> list[Critical]:
        """Stations most likely to be empty (or full) at the horizon."""
        ranked = sorted(
            (
                (s["forecasts"][horizon][f"p_{event}"], s)
                for s in store.stations()
                if horizon in s["forecasts"]
            ),
            key=lambda pair: (-pair[0], pair[1]["station_id"]),
        )
        return [
            Critical(
                station_id=s["station_id"],
                name=s["name"],
                lat=s["lat"],
                lon=s["lon"],
                probability=p,
                now=Counts(**s["now"]),
            )
            for p, s in ranked
            if p >= min_probability
        ][:limit]

    @app.get("/recommend")
    def recommend(
        lat: Annotated[float, Query(ge=48.5, le=49.2)],
        lon: Annotated[float, Query(ge=1.9, le=2.8)],
        need: Literal["bike", "dock"] = "bike",
        horizon: Horizon = "15",
        radius_m: Annotated[float, Query(gt=0, le=2000)] = 500.0,
        min_probability: Annotated[float, Query(ge=0.0, le=1.0)] = 0.8,
    ) -> Recommendation:
        """Should I go to the nearest station now, or to another one nearby?

        The chance of success is ``1 - P(empty)`` for a bike and
        ``1 - P(full)`` for a dock at the horizon (the time to walk there).
        ``best`` is the nearest station with at least ``min_probability``.
        """
        event = "empty" if need == "bike" else "full"
        options = sorted(
            (
                Option(
                    station_id=s["station_id"],
                    name=s["name"],
                    distance_m=round(distance_m(lat, lon, s["lat"], s["lon"]), 1),
                    probability=round(1.0 - s["forecasts"][horizon][f"p_{event}"], 3),
                    now=Counts(**s["now"]),
                )
                for s in store.stations()
                if horizon in s["forecasts"]
            ),
            key=lambda option: option.distance_m,
        )
        nearby = [o for o in options if o.distance_m <= radius_m]
        nearest = nearby[0] if nearby else None
        best = next((o for o in nearby if o.probability >= min_probability), None)
        return Recommendation(
            need=need,
            horizon=horizon,
            nearest=nearest,
            go_to_nearest=nearest is not None and nearest.probability >= min_probability,
            best=best,
            options=nearby,
        )

    return app


app = create_app(ForecastStore(os.environ.get(SOURCE_ENV, DEFAULT_SOURCE)))


def main() -> None:  # pragma: no cover - starts a server
    """Serve the API on ``$PORT`` (Cloud Run sets it), 8000 by default."""
    import uvicorn

    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000"))
    )  # all interfaces: inside a container
