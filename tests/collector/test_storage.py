"""Tests for the raw archive."""

import gzip
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from veloflow.collector.gbfs import RawResponse
from veloflow.collector.storage import STATUS_FORMAT_ERROR, RawArchive, iter_records

type ResponseFactory = Callable[..., RawResponse]


def test_append_writes_one_daily_file_per_feed(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    archive = RawArchive(tmp_path)
    response = make_response()

    path = archive.append("station_status", response, fingerprint="abc")

    assert path == tmp_path / "raw" / "station_status" / "station_status_2026-11-05.jsonl.gz"
    [record] = list(iter_records(path))
    assert record["fingerprint"] == "abc"
    assert record["status"] == "ok"
    assert record["fetched_at"] == "2026-11-05T08:00:00+00:00"
    assert record["content"].encode() == response.content


def test_appends_accumulate_and_days_are_split(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    archive = RawArchive(tmp_path)
    late = datetime(2026, 11, 5, 23, 59, 59, tzinfo=UTC)
    next_day = datetime(2026, 11, 6, 0, 0, 1, tzinfo=UTC)

    first = archive.append("station_status", make_response(fetched_at=late), fingerprint="a")
    second = archive.append("station_status", make_response(fetched_at=late), fingerprint="b")
    third = archive.append("station_status", make_response(fetched_at=next_day), fingerprint="c")

    assert first == second != third
    assert [r["fingerprint"] for r in iter_records(first)] == ["a", "b"]
    assert [r["fingerprint"] for r in iter_records(third)] == ["c"]


def test_format_errors_are_archived_with_their_status(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    archive = RawArchive(tmp_path)

    path = archive.append(
        "station_status", make_response(), fingerprint=None, status=STATUS_FORMAT_ERROR
    )

    [record] = list(iter_records(path))
    assert record["status"] == STATUS_FORMAT_ERROR
    assert record["fingerprint"] is None


def test_truncated_file_still_yields_complete_records(
    tmp_path: Path, make_response: ResponseFactory
) -> None:
    archive = RawArchive(tmp_path)
    path = archive.append("station_status", make_response(), fingerprint="kept")
    complete_size = path.stat().st_size
    archive.append("station_status", make_response(), fingerprint="lost")
    # Simulate a crash in the middle of the second write.
    path.write_bytes(path.read_bytes()[: complete_size + 40])

    assert [r["fingerprint"] for r in iter_records(path)] == ["kept"]


def test_corrupted_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as file:
        file.write('{"fingerprint": "ok"}\n{"fingerprint": \n{"fingerprint": "ok2"}\n')

    assert [r["fingerprint"] for r in iter_records(path)] == ["ok", "ok2"]
