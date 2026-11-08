"""Liveness signal sent to an external monitor (dead man's switch).

The monitor (healthchecks.io) expects a ping at least every few minutes and
emails us when the pings stop. The monitor must live outside the VM: a stopped
VM, a full disk or a network outage cannot report themselves.

The ping URL works like a password (anyone holding it can ping), so it comes
from the environment, never from the repository, and is never logged.
"""

import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self

import httpx

logger = logging.getLogger(__name__)

# The body of a /fail ping is stored by the monitor; keep it short.
MAX_MESSAGE_BYTES = 10_000


class Heartbeat:
    """Pings a healthchecks.io-style URL; never raises, never blocks for long."""

    def __init__(
        self,
        url: str,
        *,
        min_interval_s: float = 60.0,
        timeout_s: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the heartbeat.

        Args:
            url: ping URL (``https://hc-ping.com/<uuid>``).
            min_interval_s: at most one success ping per interval; the monitor
                only needs to know we are alive, not every 30 s.
            timeout_s: short on purpose: monitoring must not delay collection.
            transport: injected in tests.
            monotonic: injected in tests.
        """
        self._url = url.rstrip("/")
        self._min_interval_s = min_interval_s
        self._monotonic = monotonic
        self._last_ping: float | None = None
        self._http = httpx.Client(timeout=timeout_s, transport=transport)

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the HTTP client."""
        self.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def ping(self) -> None:
        """Report a healthy poll (throttled)."""
        now = self._monotonic()
        if self._last_ping is not None and now - self._last_ping < self._min_interval_s:
            return
        if self._send(self._url, ""):
            self._last_ping = now

    def fail(self, message: str) -> None:
        """Report a failure: the monitor alerts immediately, without waiting."""
        self._send(f"{self._url}/fail", message)
        # The next healthy poll pings at once, so recovery shows up immediately.
        self._last_ping = None

    def _send(self, url: str, message: str) -> bool:
        try:
            response = self._http.post(url, content=message.encode()[:MAX_MESSAGE_BYTES])
            response.raise_for_status()
        except httpx.HTTPError as error:
            # Type only: httpx error messages contain the URL, which is a secret.
            logger.warning("heartbeat not delivered (%s)", type(error).__name__)
            return False
        return True
