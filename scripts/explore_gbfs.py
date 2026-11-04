"""Explore the Vélib' Métropole GBFS feed before designing the collector.

Exploratory script, not production code. It answers four questions:

1. Which feeds does the entry point expose, and with which refresh hint (ttl)?
2. Which fields do the stations carry, with which types?
3. How often does the data really change?
4. How large is one snapshot, and what would a day of collection weigh?

Raw samples and a JSON summary are written to ``data/exploration/``
(ignored by Git). Only the standard library is used on purpose.

Usage:
    uv run python scripts/explore_gbfs.py --polls 11 --interval 30
"""

import argparse
import gzip
import itertools
import json
import logging
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GBFS_ENTRY_POINT = (
    "https://velib-metropole-opendata.smovengo.cloud/opendata/Velib_Metropole/gbfs.json"
)
# An explicit User-Agent lets the operator identify who polls their feed.
USER_AGENT = "VeloFlow-explorer/0.1 (+https://github.com/HamzaOuba00-1/veloflow)"
OUTPUT_DIR = Path("data") / "exploration"
REQUEST_TIMEOUT_S = 20.0
MINUTES_PER_DAY = 24 * 60
STALE_AFTER_S = 3600  # a station silent for more than one hour is suspicious

JsonObject = dict[str, Any]

logger = logging.getLogger("explore_gbfs")


@dataclass(frozen=True)
class Download:
    """One HTTP response: decoded JSON, raw bytes and timing."""

    payload: JsonObject
    raw: bytes
    fetched_at: datetime
    elapsed_s: float


def fetch_json(url: str) -> Download:
    """Download ``url`` and decode it as a JSON object.

    Raises:
        urllib.error.URLError: network failure or HTTP error status.
        ValueError: the body is not a JSON object.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        raw: bytes = response.read()
    elapsed = time.perf_counter() - started
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return Download(payload=payload, raw=raw, fetched_at=datetime.now(UTC), elapsed_s=elapsed)


def feed_timestamp(payload: JsonObject) -> int | None:
    """Return the feed generation time (Unix seconds), whatever its key name."""
    # The GBFS standard says ``last_updated``; some operators use other names.
    for key in ("last_updated", "lastUpdatedOther"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    return None


def discover_feeds(entry_point: JsonObject) -> dict[str, str]:
    """Map feed name to URL from the ``gbfs.json`` entry point."""
    data = entry_point.get("data", {})
    # GBFS 1.x / 2.x group feeds by language ({"en": {"feeds": [...]}});
    # GBFS 3.x puts them directly under data.feeds.
    feeds = data["feeds"] if "feeds" in data else next(iter(data.values()))["feeds"]
    return {str(feed["name"]): str(feed["url"]) for feed in feeds}


def stations_of(payload: JsonObject) -> list[JsonObject]:
    """Return the station list of a station_status / station_information feed."""
    stations = payload.get("data", {}).get("stations", [])
    return [station for station in stations if isinstance(station, dict)]


def field_inventory(stations: list[JsonObject]) -> dict[str, list[str]]:
    """List every field seen across stations with the Python types it takes."""
    types: defaultdict[str, set[str]] = defaultdict(set)
    for station in stations:
        for key, value in station.items():
            types[key].add(type(value).__name__)
    return {key: sorted(found) for key, found in sorted(types.items())}


def bikes_by_type(station: JsonObject) -> dict[str, int]:
    """Count available bikes per type (mechanical, ebike, ...).

    GBFS 1.x operators often encode this as a list of one-key dicts,
    e.g. ``[{"mechanical": 3}, {"ebike": 1}]``; a plain dict is accepted too.
    """
    raw = station.get("num_bikes_available_types")
    items: list[Any] = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    counts: Counter[str] = Counter()
    for item in items:
        if isinstance(item, dict):
            for bike_type, count in item.items():
                if isinstance(count, int):
                    counts[str(bike_type)] += count
    return dict(counts)


def as_int(value: Any) -> int | None:
    """Return ``value`` if it is an int (bools excluded), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def state_fingerprint(station: JsonObject) -> tuple[Any, ...]:
    """What a user would notice: stock per bike type, free docks and service flags.

    ``last_reported`` is left out on purpose: on Vélib' it does not follow state
    updates (see docs/data/gbfs_exploration.md), so it would distort change counts.
    """
    bikes = tuple(sorted(bikes_by_type(station).items()))
    return (
        bikes,
        station.get("num_docks_available"),
        station.get("is_renting"),
        station.get("is_returning"),
        station.get("is_installed"),
    )


def analyse_snapshot(
    status: list[JsonObject], information: list[JsonObject], fetched_at: datetime
) -> JsonObject:
    """Data-quality indicators for one station_status snapshot."""
    capacity = {s.get("station_id"): as_int(s.get("capacity")) for s in information}
    status_ids = {s.get("station_id") for s in status}

    flags = {
        flag: dict(Counter(str(s.get(flag)) for s in status))
        for flag in ("is_installed", "is_renting", "is_returning")
    }

    type_sum_mismatch = 0
    over_capacity = 0
    empty = 0
    full = 0
    for station in status:
        total = as_int(station.get("num_bikes_available"))
        docks = as_int(station.get("num_docks_available"))
        by_type = bikes_by_type(station)
        if total is not None and by_type and sum(by_type.values()) != total:
            type_sum_mismatch += 1
        cap = capacity.get(station.get("station_id"))
        if total is not None and docks is not None and cap is not None and total + docks > cap:
            over_capacity += 1
        if total == 0:
            empty += 1
        if docks == 0:
            full += 1

    now_s = fetched_at.timestamp()
    reported_times = [as_int(s.get("last_reported")) for s in status]
    ages = [now_s - reported for reported in reported_times if reported is not None]
    age_summary: JsonObject = {}
    if ages:
        deciles = statistics.quantiles(ages, n=10) if len(ages) > 1 else [ages[0]] * 9
        age_summary = {
            "median_s": round(statistics.median(ages)),
            "p90_s": round(deciles[-1]),
            "max_s": round(max(ages)),
            f"stations_older_than_{STALE_AFTER_S}s": sum(a > STALE_AFTER_S for a in ages),
        }

    return {
        "stations_in_status": len(status),
        "stations_in_information": len(information),
        "only_in_status": len(status_ids - capacity.keys()),
        "only_in_information": len(capacity.keys() - status_ids),
        "flags": flags,
        "bike_types_seen": sorted({t for s in status for t in bikes_by_type(s)}),
        "type_sum_mismatch": type_sum_mismatch,
        "bikes_plus_docks_above_capacity": over_capacity,
        "empty_stations": empty,
        "full_stations": full,
        "last_reported_age": age_summary,
    }


def poll_status(url: str, polls: int, interval_s: float) -> JsonObject:
    """Poll station_status and measure how often the feed and the stations change."""
    previous: dict[Any, tuple[Any, ...]] = {}
    feed_times: list[int] = []
    changes_per_poll: list[int] = []
    first: Download | None = None
    last: Download | None = None

    for index in range(polls):
        if index:
            time.sleep(interval_s)
        download = fetch_json(url)
        first = first or download
        last = download
        stamp = feed_timestamp(download.payload)
        if stamp is not None:
            feed_times.append(stamp)
        current = {s.get("station_id"): state_fingerprint(s) for s in stations_of(download.payload)}
        if previous:
            changed = sum(1 for key, value in current.items() if previous.get(key) != value)
            changes_per_poll.append(changed)
        previous = current
        logger.info(
            "poll %d/%d: feed timestamp=%s, %s stations changed, %.2fs",
            index + 1,
            polls,
            stamp,
            changes_per_poll[-1] if changes_per_poll else "-",
            download.elapsed_s,
        )

    if first is None or last is None:  # unreachable while polls >= 1
        raise ValueError("at least one poll is required")
    _save(first, "station_status_first.json")
    _save(last, "station_status_last.json")

    distinct_times = sorted(set(feed_times))
    gaps = [b - a for a, b in itertools.pairwise(distinct_times)]
    mean_changes = statistics.mean(changes_per_poll) if changes_per_poll else 0.0
    changes_per_minute = mean_changes * 60 / interval_s
    return {
        "polls": polls,
        "interval_s": interval_s,
        "distinct_feed_timestamps": len(distinct_times),
        "feed_refresh_gaps_s": gaps,
        "changed_stations_per_poll": changes_per_poll,
        "estimated_changed_stations_per_minute": round(changes_per_minute, 1),
        "estimated_change_rows_per_day": round(changes_per_minute * MINUTES_PER_DAY),
    }


def volume_estimates(snapshot: Download) -> JsonObject:
    """Daily and monthly size if we kept one full snapshot per minute."""
    raw_bytes = len(snapshot.raw)
    gzip_bytes = len(gzip.compress(snapshot.raw))
    per_day_gzip = gzip_bytes * MINUTES_PER_DAY
    return {
        "snapshot_raw_kb": round(raw_bytes / 1024, 1),
        "snapshot_gzip_kb": round(gzip_bytes / 1024, 1),
        "compression_ratio": round(raw_bytes / gzip_bytes, 1),
        "full_snapshots_raw_mb_per_day": round(raw_bytes * MINUTES_PER_DAY / 1024**2),
        "full_snapshots_gzip_mb_per_day": round(per_day_gzip / 1024**2),
        "full_snapshots_gzip_gb_per_30_days": round(per_day_gzip * 30 / 1024**3, 2),
    }


def _save(download: Download, name: str) -> None:
    """Write raw bytes exactly as received, for later inspection and test fixtures."""
    (OUTPUT_DIR / name).write_bytes(download.raw)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--polls", type=int, default=11, help="number of station_status calls")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between calls")
    args = parser.parse_args(argv)
    if args.polls < 1 or args.interval <= 0:
        parser.error("--polls must be >= 1 and --interval > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the exploration and write ``data/exploration/summary.json``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        entry = fetch_json(GBFS_ENTRY_POINT)
        _save(entry, "gbfs.json")
        feeds = discover_feeds(entry.payload)
        logger.info("feeds: %s", ", ".join(sorted(feeds)))

        information = fetch_json(feeds["station_information"])
        _save(information, "station_information.json")
        status = fetch_json(feeds["station_status"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
        logger.error("exploration aborted: %s", error)
        return 1

    info_stations = stations_of(information.payload)
    status_stations = stations_of(status.payload)
    summary: JsonObject = {
        "explored_at_utc": status.fetched_at.isoformat(timespec="seconds"),
        "entry_point": {
            "top_level_keys": sorted(entry.payload),
            "version": entry.payload.get("version"),
            "ttl_s": entry.payload.get("ttl"),
            "feeds": feeds,
        },
        "station_information": {
            "top_level_keys": sorted(information.payload),
            "ttl_s": information.payload.get("ttl"),
            "fields": field_inventory(info_stations),
            "example": info_stations[0] if info_stations else None,
        },
        "station_status": {
            "top_level_keys": sorted(status.payload),
            "ttl_s": status.payload.get("ttl"),
            "response_time_s": round(status.elapsed_s, 2),
            "fields": field_inventory(status_stations),
            "example": status_stations[0] if status_stations else None,
        },
        "quality": analyse_snapshot(status_stations, info_stations, status.fetched_at),
        "volume": volume_estimates(status),
    }

    try:
        summary["dynamics"] = poll_status(feeds["station_status"], args.polls, args.interval)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        logger.error("polling aborted, summary written without dynamics: %s", error)

    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("summary written to %s", summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
