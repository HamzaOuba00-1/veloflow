"""Tests for the collection loop, with a scripted feed source (no network)."""

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from veloflow.collector import service
from veloflow.collector.gbfs import (
    FeedFormatError,
    FeedUnavailableError,
    RawResponse,
    parse_station_information,
    parse_station_status,
)
from veloflow.collector.service import Collector, fingerprint, next_deadline
from veloflow.collector.storage import RawArchive, iter_records

type Step = RawResponse | Exception
type ResponseFactory = Callable[..., RawResponse]


class ScriptedSource:
    """Plays a fixed sequence of responses or errors per feed."""

    def __init__(self, script: dict[str, list[Step]]) -> None:
        self._script = {feed: deque(steps) for feed, steps in script.items()}
        self.fetched: list[str] = []
        self.rediscoveries = 0

    def fetch(self, feed_name: str) -> RawResponse:
        self.fetched.append(feed_name)
        step = self._script[feed_name].popleft()
        if isinstance(step, Exception):
            raise step
        return step

    def forget_feed_urls(self) -> None:
        self.rediscoveries += 1


def make_collector(
    source: ScriptedSource, data_dir: Path, *, poll_interval_s: float = 30.0
) -> Collector:
    return Collector(
        source,
        RawArchive(data_dir),
        poll_interval_s=poll_interval_s,
        information_interval_s=3600.0,
    )


def archived(data_dir: Path, feed: str = "station_status") -> list[dict[str, Any]]:
    return [r for path in sorted(data_dir.glob(f"raw/{feed}/*.gz")) for r in iter_records(path)]


def change_first_station(feed: dict[str, Any]) -> None:
    feed["data"]["stations"][0]["num_docks_available"] -= 1
    feed["data"]["stations"][0]["num_bikes_available"] += 1
    feed["data"]["stations"][0]["num_bikes_available_types"][0]["mechanical"] += 1


def older_timestamp(feed: dict[str, Any]) -> None:
    feed["lastUpdatedOther"] -= 1


def other_server(feed: dict[str, Any]) -> None:
    """Same stock as seen from the second server: older last_reported values."""
    feed["lastUpdatedOther"] += 2
    for station in feed["data"]["stations"][:3]:
        station["last_reported"] -= 603


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (0.0, 30.0),  # right at the start: next slot
        (3.2, 30.0),  # a 3.2 s request does not shift the grid
        (29.9, 30.0),
        (30.0, 60.0),
        (75.0, 90.0),  # overran past 60: slot 60 is skipped, not run late
    ],
)
def test_next_deadline_follows_a_fixed_grid(now: float, expected: float) -> None:
    assert next_deadline(start=0.0, now=now, interval=30.0) == pytest.approx(expected)


def test_fingerprint_ignores_feed_timestamps(make_response: ResponseFactory) -> None:
    base = make_response()
    lagging = make_response(edit=older_timestamp)
    second_server = make_response(edit=other_server)
    changed = make_response(edit=change_first_station)

    def fp(response: RawResponse) -> str:
        return fingerprint(parse_station_status(response.content, response.fetched_at))

    assert fp(base) == fp(lagging)
    assert fp(base) == fp(second_server)  # regression: observed on 5 November 2026
    assert fp(base) != fp(changed)


def test_fingerprint_of_information_uses_every_field(make_response: ResponseFactory) -> None:
    def rename(feed: dict[str, Any]) -> None:
        feed["data"]["stations"][0]["name"] = "Renamed station"

    def fp(response: RawResponse) -> str:
        return fingerprint(parse_station_information(response.content, response.fetched_at))

    assert fp(make_response("station_information.json")) != fp(
        make_response("station_information.json", edit=rename)
    )


# --------------------------------------------------------------------------
# One poll
# --------------------------------------------------------------------------


def test_first_version_is_archived(tmp_path: Path, make_response: ResponseFactory) -> None:
    collector = make_collector(ScriptedSource({"station_status": [make_response()]}), tmp_path)

    assert collector.poll_status() is True
    [record] = archived(tmp_path)
    assert record["status"] == "ok"
    assert len(record["fingerprint"]) == 64


def test_same_content_with_older_timestamp_is_skipped(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    """The two-servers case observed during exploration."""
    source = ScriptedSource(
        {"station_status": [make_response(), make_response(edit=older_timestamp)]}
    )
    collector = make_collector(source, tmp_path)

    assert collector.poll_status() is True
    assert collector.poll_status() is False
    assert len(archived(tmp_path)) == 1


def test_lagging_server_cannot_reintroduce_an_old_version(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    old, new = make_response(), make_response(edit=change_first_station)
    source = ScriptedSource({"station_status": [old, new, old, new]})
    collector = make_collector(source, tmp_path)

    results = [collector.poll_status() for _ in range(4)]

    assert results == [True, True, False, False]
    assert len(archived(tmp_path)) == 2


def test_unavailable_feed_is_counted_and_triggers_rediscovery(
    tmp_path: Path, make_response: ResponseFactory, caplog: pytest.LogCaptureFixture
) -> None:
    failures: list[Any] = [FeedUnavailableError("503")] * service.ALERT_AFTER_FAILURES
    source = ScriptedSource({"station_status": [*failures, make_response()]})
    collector = make_collector(source, tmp_path)

    for _ in range(service.ALERT_AFTER_FAILURES):
        assert collector.poll_status() is False

    assert source.rediscoveries == service.ALERT_AFTER_FAILURES // service.REDISCOVER_EVERY
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert collector.poll_status() is True  # recovers as soon as the feed is back


def test_feed_missing_from_discovery_is_a_failure(tmp_path: Path) -> None:
    source = ScriptedSource({"station_status": [FeedFormatError("not listed")]})

    assert make_collector(source, tmp_path).poll_status() is False
    assert archived(tmp_path) == []


def test_unparseable_response_is_kept_as_evidence(tmp_path: Path) -> None:
    page = RawResponse(b"<html>maintenance</html>", datetime(2026, 11, 5, tzinfo=UTC))
    source = ScriptedSource({"station_status": [page]})

    assert make_collector(source, tmp_path).poll_status() is False
    [record] = archived(tmp_path)
    assert record["status"] == "format_error"
    assert record["content"] == "<html>maintenance</html>"


def test_rejected_stations_are_logged(
    tmp_path: Path, make_response: ResponseFactory, caplog: pytest.LogCaptureFixture
) -> None:
    def break_one(feed: dict[str, Any]) -> None:
        # 27 records, 1 broken: 3.7 %, below the degradation threshold.
        feed["data"]["stations"] = feed["data"]["stations"] * 3
        feed["data"]["stations"][0] = {**feed["data"]["stations"][0], "num_docks_available": -1}

    source = ScriptedSource({"station_status": [make_response(edit=break_one)]})

    assert make_collector(source, tmp_path).poll_status() is True
    assert "1 station records rejected" in caplog.text


def null_codes_and_empty_types(feed: dict[str, Any]) -> None:
    """The degraded version observed on 5 November 2026 at 09:45 UTC."""
    for station in feed["data"]["stations"]:
        station["stationCode"] = None
        station["num_bikes_available_types"] = [{}, {}]


def test_degraded_version_is_kept_as_evidence_but_not_as_data(
    tmp_path: Path, make_response: ResponseFactory, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(
        {"station_status": [make_response(edit=null_codes_and_empty_types), make_response()]}
    )
    collector = make_collector(source, tmp_path)

    assert collector.poll_status() is False
    assert collector.poll_status() is True  # the next good version is archived

    records = archived(tmp_path)
    assert [r["status"] for r in records] == ["degraded", "ok"]
    assert records[0]["fingerprint"] is None
    assert "degraded version: 9/9 rejected" in caplog.text


def test_information_feed_is_archived_separately(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    source = ScriptedSource({"station_information": [make_response("station_information.json")]})

    assert make_collector(source, tmp_path).poll_information() is True
    assert len(archived(tmp_path, "station_information")) == 1


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_run_polls_both_feeds_until_stopped(tmp_path: Path, make_response: ResponseFactory) -> None:
    source = ScriptedSource(
        {
            "station_information": [make_response("station_information.json")],
            "station_status": [make_response(), make_response(edit=change_first_station)],
        }
    )
    collector = make_collector(source, tmp_path, poll_interval_s=0.01)
    original = collector.poll_status

    def poll_then_stop_after_two() -> bool:
        result = original()
        if source.fetched.count("station_status") == 2:
            collector.stop()
        return result

    collector.poll_status = poll_then_stop_after_two  # type: ignore[method-assign]
    collector.run()

    assert source.fetched == ["station_information", "station_status", "station_status"]
    assert len(archived(tmp_path)) == 2


def test_unexpected_error_does_not_stop_the_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    collector = make_collector(ScriptedSource({}), tmp_path, poll_interval_s=0.01)
    calls = 0

    def buggy_poll() -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            collector.stop()
        raise RuntimeError("bug")

    collector.poll_status = buggy_poll  # type: ignore[method-assign]
    collector.poll_information = lambda: False  # type: ignore[method-assign]
    collector.run()

    assert calls == 2
    assert "unexpected error" in caplog.text
