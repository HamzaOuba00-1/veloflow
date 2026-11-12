"""Where the API and the app read the forecast document, and how they keep it.

The document is written every 10 minutes by ``veloflow-predict`` to a file
or a bucket. Both readers go through ``ForecastStore``: it reloads the
document at most once per ``ttl`` seconds, checks it before using it, and
keeps serving the last good one when a reload fails (bucket unreachable, file
being replaced, broken document). Only with no good document at all does it
raise ``ForecastsUnavailableError``, which the API turns into a 503 and the app
into a message.
"""

import json
import math
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

SOURCE_ENV = "VELOFLOW_FORECASTS"
DEFAULT_SOURCE = "data/serving/forecasts.json"
TTL_S = 60.0

TOP_KEYS = ("generated_at", "origin", "model", "stations")
STATION_KEYS = ("station_id", "name", "lat", "lon", "now", "forecasts")


class ForecastsUnavailableError(RuntimeError):
    """No usable forecast document could be read."""


def read_source(source: str) -> Any:
    """The parsed JSON at a local path or an ``http(s)`` URL (not yet checked)."""
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, timeout=10.0)
        response.raise_for_status()
        return response.json()
    return json.loads(Path(source).read_text(encoding="utf-8"))


def check_document(document: Any) -> dict[str, Any]:
    """``document`` if the API and the app can use it, else a ``ValueError``.

    Checks the shape the readers rely on, not the forecasts themselves: those
    were checked by ``veloflow-predict`` when writing.
    """
    if not isinstance(document, dict):
        raise ValueError("the document is not a JSON object")
    missing = [key for key in TOP_KEYS if key not in document]
    if missing:
        raise ValueError(f"the document has no {', '.join(missing)}")
    for key in ("generated_at", "origin"):
        datetime.fromisoformat(str(document[key]))  # ValueError when malformed
    stations = document["stations"]
    if not isinstance(stations, list) or not stations:
        raise ValueError("the document has no station")
    for station in stations:
        if not isinstance(station, dict) or any(k not in station for k in STATION_KEYS):
            raise ValueError("a station of the document is incomplete")
        if not isinstance(station["forecasts"], dict) or not station["forecasts"]:
            raise ValueError(f"station {station['station_id']} has no forecast")
    return document


class ForecastStore:
    """The current document, reloaded at most once per ``ttl`` seconds."""

    def __init__(
        self,
        source: str,
        ttl: float = TTL_S,
        reader: Callable[[str], Any] = read_source,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Remember where to read; nothing is read before the first request."""
        self.source, self.ttl, self.reader, self.clock = source, ttl, reader, clock
        self._document: dict[str, Any] | None = None
        self._loaded_at = -math.inf
        self.last_error: str | None = None

    def document(self) -> dict[str, Any]:
        """The cached document, refreshed when older than ``ttl``.

        A failed refresh keeps serving the previous document (and is kept in
        ``last_error``); with none, ``ForecastsUnavailableError`` is raised.
        """
        if self.clock() - self._loaded_at >= self.ttl:
            try:
                self._document = check_document(self.reader(self.source))
                self.last_error = None
            except (OSError, ValueError, httpx.HTTPError) as error:
                self.last_error = str(error) or type(error).__name__
                if self._document is None:
                    raise ForecastsUnavailableError(self.last_error) from error
            # Also after a failure: an unreachable source is retried once per
            # ttl, not on every request (each try may wait for a timeout).
            self._loaded_at = self.clock()
        assert self._document is not None
        return self._document

    def stations(self) -> list[dict[str, Any]]:
        """Stations of the current document."""
        stations: list[dict[str, Any]] = self.document()["stations"]
        return stations
