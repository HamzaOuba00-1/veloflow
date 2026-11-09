"""Tests for the hourly forecast snapshot."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from veloflow.collector.storage import RawArchive, iter_records
from veloflow.weather.grid import GRID
from veloflow.weather.openmeteo import OpenMeteoClient, WeatherFormatError
from veloflow.weather.snapshot import FEED, main, take_snapshot


def serving(content: bytes, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _: httpx.Response(status, content=content))


def test_snapshot_is_archived_in_bronze(tmp_path: Path, body: Callable[..., bytes]) -> None:
    content = body(len(GRID))
    with OpenMeteoClient(user_agent="test", transport=serving(content)) as client:
        path = take_snapshot(client, RawArchive(tmp_path))

    assert path.parent == tmp_path / "raw" / FEED
    [record] = list(iter_records(path))
    assert record["status"] == "ok"
    assert record["fingerprint"] == hashlib.sha256(content).hexdigest()
    assert record["content"].encode() == content


def test_unexpected_response_is_kept_as_evidence(
    tmp_path: Path, body: Callable[..., bytes]
) -> None:
    archive = RawArchive(tmp_path)
    # A valid location, but one instead of nine: the grid changed upstream?
    with (
        OpenMeteoClient(user_agent="test", transport=serving(body(1))) as client,
        pytest.raises(WeatherFormatError),
    ):
        take_snapshot(client, archive)

    [path] = (tmp_path / "raw" / FEED).iterdir()
    [record] = list(iter_records(path))
    assert record["status"] == "format_error"


def test_main_succeeds(tmp_path: Path, body: Callable[..., bytes]) -> None:
    code = main(["--data-dir", str(tmp_path)], transport=serving(body(len(GRID))))

    assert code == 0
    assert len(list((tmp_path / "raw" / FEED).iterdir())) == 1


def test_main_reports_failure_with_exit_code(tmp_path: Path) -> None:
    code = main(["--data-dir", str(tmp_path)], transport=serving(b"busy", status=503))

    assert code == 1
    assert not (tmp_path / "raw").exists()
