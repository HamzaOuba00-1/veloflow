"""Append-only archive of raw feed responses (bronze layer, decision D10).

One gzip-compressed JSON Lines file per feed and per UTC day::

    data/raw/station_status/station_status_2026-11-05.jsonl.gz

Each line holds the response body exactly as received plus our metadata.
Appending opens a new gzip member each time: the format stays valid, any gzip
reader decompresses it, and a crash can at worst truncate the last line.
"""

import gzip
import json
import logging
import zlib
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from veloflow.collector.gbfs import RawResponse

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_FORMAT_ERROR = "format_error"
# Parsable envelope, but too many station records rejected to trust the version.
STATUS_DEGRADED = "degraded"


class RawArchive:
    """Writes and reads the raw archive under ``<data_dir>/raw``."""

    def __init__(self, data_dir: Path) -> None:
        """Create the archive rooted at ``data_dir``."""
        self._root = data_dir / "raw"

    def path_for(self, feed: str, day: date) -> Path:
        """Daily file of ``feed`` for the UTC ``day``."""
        return self._root / feed / f"{feed}_{day.isoformat()}.jsonl.gz"

    def append(
        self,
        feed: str,
        response: RawResponse,
        *,
        fingerprint: str | None,
        status: str = STATUS_OK,
    ) -> Path:
        """Append one response; return the file written to.

        Responses that failed parsing are kept too (``status="format_error"``):
        they are the evidence we need when the operator changes its format.
        """
        path = self.path_for(feed, response.fetched_at.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "fetched_at": response.fetched_at.isoformat(),
            "fingerprint": fingerprint,
            "status": status,
            # Stored as text: the feed is UTF-8 JSON. Undecodable bytes (never
            # observed) would be replaced rather than crash the collector.
            "content": response.content.decode("utf-8", errors="replace"),
        }
        with gzip.open(path, "at", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of one archive file, tolerating a truncated tail.

    A file cut short by a crash or a power loss still yields every complete
    record before the damage.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as file:
            for line in file:
                try:
                    record: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("%s: skipping a truncated line", path)
                    continue
                yield record
    except (EOFError, gzip.BadGzipFile, zlib.error):
        logger.warning("%s: file ends with an incomplete gzip block", path)
