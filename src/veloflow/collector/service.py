"""The collection loop.

Every ``poll_interval_s`` seconds, on a fixed grid, the collector downloads
``station_status``, archives it if its content is new, and refreshes
``station_information`` every ``information_interval_s`` seconds.
"""

import dataclasses
import hashlib
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from veloflow.collector.gbfs import (
    FeedFormatError,
    FeedUnavailableError,
    RawResponse,
    parse_station_information,
    parse_station_status,
)
from veloflow.collector.models import FeedSnapshot, StationInfo, StationStatus
from veloflow.collector.storage import STATUS_DEGRADED, STATUS_FORMAT_ERROR, RawArchive

logger = logging.getLogger(__name__)

STATUS_FEED = "station_status"
INFORMATION_FEED = "station_information"

# After this many failures in a row, warnings become errors (5 min at 30 s).
ALERT_AFTER_FAILURES = 10
# Every this many failures, re-read gbfs.json in case a feed URL moved.
REDISCOVER_EVERY = 3
# Feed versions remembered for deduplication. More than one, because several
# servers answer behind the same address and one of them may lag behind.
RECENT_VERSIONS = 8
# Above this share of rejected station records, a version is "degraded": kept as
# evidence but not treated as data. Observed on 5 November 2026: one version in
# 17 came with every stationCode null and empty bike types.
MAX_REJECTED_SHARE = 0.10


class FeedSource(Protocol):
    """What the collector needs from a feed client (``GbfsClient`` in production)."""

    def fetch(self, feed_name: str) -> RawResponse:
        """Download one feed."""
        ...

    def forget_feed_urls(self) -> None:
        """Force feed auto-discovery on the next call."""
        ...


def _observable_state(station: StationStatus | StationInfo) -> tuple[object, ...]:
    """What defines a new version of a station: everything but timestamps.

    ``last_reported`` is excluded on purpose. Measured on 5 November 2026: the
    operator's servers publish the same stock with ``last_reported`` values up
    to ten minutes apart for ~126 stations. Including it made every server
    switch look like a new version.
    """
    if isinstance(station, StationStatus):
        return (
            station.station_id,
            station.mechanical,
            station.ebike,
            station.docks,
            station.is_installed,
            station.is_renting,
            station.is_returning,
        )
    return dataclasses.astuple(station)


def fingerprint(snapshot: FeedSnapshot[StationStatus] | FeedSnapshot[StationInfo]) -> str:
    """Hash of the observable station states, ignoring every timestamp.

    Two responses with the same stock but different ``lastUpdatedOther`` or
    ``last_reported`` (two servers behind one address) get the same fingerprint.
    """
    digest = hashlib.sha256()
    rows = sorted(repr(_observable_state(station)) for station in snapshot.stations)
    for row in rows:
        digest.update(row.encode())
    return digest.hexdigest()


def next_deadline(start: float, now: float, interval: float) -> float:
    """First instant of the grid ``start + k * interval`` strictly after ``now``.

    Sleeping "interval seconds after the previous call" would drift by the
    request duration (~3 s on Vélib'). A fixed grid does not, and silently
    skips the slots missed when a call overruns.
    """
    return start + (math.floor((now - start) / interval) + 1) * interval


@dataclasses.dataclass
class _FeedState:
    recent: deque[str] = dataclasses.field(default_factory=lambda: deque(maxlen=RECENT_VERSIONS))
    consecutive_failures: int = 0


class Collector:
    """Polls the feeds and writes new versions to the raw archive."""

    def __init__(
        self,
        source: FeedSource,
        archive: RawArchive,
        *,
        poll_interval_s: float,
        information_interval_s: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wire the collector; nothing runs until :meth:`run`."""
        self._source = source
        self._archive = archive
        self._poll_interval_s = poll_interval_s
        self._information_interval_s = information_interval_s
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._states = {STATUS_FEED: _FeedState(), INFORMATION_FEED: _FeedState()}

    def stop(self) -> None:
        """Ask the loop to finish after the current poll (safe from a signal handler)."""
        self._stop.set()

    def run(self) -> None:
        """Poll until :meth:`stop` is called."""
        start = self._monotonic()
        next_information = start
        while not self._stop.is_set():
            if self._monotonic() >= next_information:
                self._guarded(self.poll_information)
                next_information = self._monotonic() + self._information_interval_s
            self._guarded(self.poll_status)
            deadline = next_deadline(start, self._monotonic(), self._poll_interval_s)
            # Event.wait instead of time.sleep: a stop request wakes us up at once.
            self._stop.wait(max(0.0, deadline - self._monotonic()))

    def poll_status(self) -> bool:
        """Poll ``station_status`` once; return True if a new version was archived."""
        return self._poll(STATUS_FEED, parse_station_status)

    def poll_information(self) -> bool:
        """Poll ``station_information`` once; return True if a new version was archived."""
        return self._poll(INFORMATION_FEED, parse_station_information)

    def _guarded(self, poll: Callable[[], bool]) -> None:
        """Run one poll; an unexpected bug is logged but never stops the loop."""
        try:
            poll()
        except Exception:
            logger.exception("unexpected error during a poll; the loop continues")

    def _poll[T: (StationStatus, StationInfo)](
        self, feed: str, parse: Callable[[bytes, datetime], FeedSnapshot[T]]
    ) -> bool:
        state = self._states[feed]
        try:
            response = self._source.fetch(feed)
        except FeedUnavailableError as error:
            self._record_failure(feed, state, error)
            return False
        except FeedFormatError as error:  # feed missing from gbfs.json
            self._record_failure(feed, state, error)
            return False

        try:
            snapshot = parse(response.content, response.fetched_at)
        except FeedFormatError as error:
            # Keep the evidence: this response tells us how the format changed.
            self._archive.append(feed, response, fingerprint=None, status=STATUS_FORMAT_ERROR)
            self._record_failure(feed, state, error)
            return False

        total = len(snapshot.stations) + snapshot.rejected
        if total == 0 or snapshot.rejected / total > MAX_REJECTED_SHARE:
            self._archive.append(feed, response, fingerprint=None, status=STATUS_DEGRADED)
            degraded = FeedFormatError(f"degraded version: {snapshot.rejected}/{total} rejected")
            self._record_failure(feed, state, degraded)
            return False

        state.consecutive_failures = 0
        version = fingerprint(snapshot)
        if version in state.recent:
            logger.debug("%s: version already archived, skipped", feed)
            return False

        self._archive.append(feed, response, fingerprint=version)
        state.recent.append(version)
        logger.info(
            "%s: new version, feed time %s, %d stations, %d rejected",
            feed,
            snapshot.feed_updated_at.isoformat(),
            len(snapshot.stations),
            snapshot.rejected,
        )
        if snapshot.rejected:
            logger.warning("%s: %d station records rejected", feed, snapshot.rejected)
        return True

    def _record_failure(self, feed: str, state: _FeedState, error: Exception) -> None:
        state.consecutive_failures += 1
        failures = state.consecutive_failures
        level = logging.ERROR if failures >= ALERT_AFTER_FAILURES else logging.WARNING
        logger.log(level, "%s: failure %d in a row: %s", feed, failures, error)
        if failures % REDISCOVER_EVERY == 0:
            self._source.forget_feed_urls()
