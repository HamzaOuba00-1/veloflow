"""Tests for live forecasting from the raw archive."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from veloflow.collector.gbfs import RawResponse
from veloflow.collector.service import STATUS_FEED
from veloflow.collector.storage import STATUS_DEGRADED, STATUS_FORMAT_ERROR, RawArchive
from veloflow.features.build import make_context
from veloflow.models.gbm import GradientBoosting
from veloflow.models.train import train
from veloflow.serving import live
from veloflow.serving.live import (
    _fetched_at,
    latest_forecast,
    live_examples,
    main,
    recent_status,
)
from veloflow.serving.monitor import Monitor
from veloflow.weather.snapshot import FEED as WEATHER_FEED

NOW = datetime(2026, 11, 12, 8, 3, 30, tzinfo=UTC)
STATIONS = pl.DataFrame(
    {"station_id": [1, 2], "lat": [48.8566, 48.8570], "lon": [2.3522, 2.3530]},
    schema={"station_id": pl.Int64, "lat": pl.Float64, "lon": pl.Float64},
)


def status_at(make_response: Callable[..., RawResponse], minutes_ago: float) -> RawResponse:
    """The recorded feed, fetched ``minutes_ago`` before NOW, with a matching feed time."""
    fetched = NOW - timedelta(minutes=minutes_ago)

    def edit(feed: dict[str, Any]) -> None:
        feed["lastUpdatedOther"] = int(fetched.timestamp())

    return make_response(edit=edit, fetched_at=fetched)


def archive_status(data_dir: Path, make_response: Callable[..., RawResponse]) -> None:
    archive = RawArchive(data_dir)
    for minutes, fingerprint, status in (
        (80.0, "old", "ok"),  # outside the 70-minute window
        (30.0, "a", "ok"),
        (12.0, "b", STATUS_DEGRADED),  # not usable
        (6.0, "c", "ok"),
        (6.0, "c", "ok"),  # same version archived twice
        (1.0, "d", "ok"),
    ):
        archive.append(
            STATUS_FEED, status_at(make_response, minutes), fingerprint=fingerprint, status=status
        )
    archive.append(
        STATUS_FEED,
        RawResponse(content=b"not json", fetched_at=NOW - timedelta(minutes=2)),
        fingerprint="e",
    )


def test_fetch_time_is_read_without_decoding() -> None:
    assert _fetched_at('{"fetched_at": "2026-11-12T08:00:00+00:00", "x": 1}') == datetime(
        2026, 11, 12, 8, tzinfo=UTC
    )
    assert _fetched_at('{"other": 1}') is None
    assert _fetched_at('{"fetched_at": "yesterday"}') is None


def test_recent_status_keeps_the_usable_versions_of_the_window(
    tmp_path: Path, make_response: Callable[..., RawResponse]
) -> None:
    archive_status(tmp_path, make_response)

    status = recent_status(tmp_path, NOW)

    times = sorted(status["fetched_at"].unique().to_list())
    assert times == [NOW - timedelta(minutes=m) for m in (30, 6, 1)]
    assert "operational" in status.columns
    assert recent_status(tmp_path / "empty", NOW).height == 0


def test_recent_status_reads_yesterday_after_midnight(
    tmp_path: Path, make_response: Callable[..., RawResponse]
) -> None:
    archive = RawArchive(tmp_path)
    late = make_response(fetched_at=datetime(2026, 11, 11, 23, 50, tzinfo=UTC))
    archive.append(STATUS_FEED, late, fingerprint="late")

    status = recent_status(tmp_path, datetime(2026, 11, 12, 0, 20, tzinfo=UTC))

    assert status["fetched_at"].unique().to_list() == [late.fetched_at]


def test_latest_forecast_takes_the_newest_parsable_snapshot(
    tmp_path: Path, body: Callable[..., bytes]
) -> None:
    archive = RawArchive(tmp_path)
    assert latest_forecast(tmp_path, NOW) is None
    archive.append(
        WEATHER_FEED,
        RawResponse(content=body(9), fetched_at=NOW - timedelta(hours=1)),
        fingerprint="w1",
    )
    archive.append(
        WEATHER_FEED,
        RawResponse(content=b"{}", fetched_at=NOW - timedelta(minutes=50)),
        fingerprint="w2",
        status=STATUS_FORMAT_ERROR,
    )
    archive.append(
        WEATHER_FEED,
        RawResponse(content=b'{"broken": true}', fetched_at=NOW - timedelta(minutes=40)),
        fingerprint="w3",
    )

    weather = latest_forecast(tmp_path, NOW)

    assert weather is not None
    assert weather["point"].n_unique() == 9
    assert "temperature_2m" in weather.columns


def test_live_examples_at_the_latest_grid_time(
    tmp_path: Path, make_response: Callable[..., RawResponse]
) -> None:
    archive_status(tmp_path, make_response)

    examples = live_examples(recent_status(tmp_path, NOW))

    # Last version 08:02:30; the grid time is 08:00, holding the 07:57:30 state.
    assert examples["origin"].unique().to_list() == [datetime(2026, 11, 12, 8, tzinfo=UTC)]
    assert sorted(examples["horizon_min"].unique().to_list()) == [15, 30, 60]
    assert live_examples(recent_status(tmp_path / "empty", NOW)).height == 0


@pytest.fixture
def served(
    silver_dir: Path, make_response: Callable[..., RawResponse], body: Callable[..., bytes]
) -> Path:
    """A data folder with a small trained model and a recent raw archive."""
    small: dict[str, object] = {"n_estimators": 5, "num_leaves": 7, "min_child_samples": 5}
    train(silver_dir, NOW, GradientBoosting(make_context(STATIONS), params=small))
    archive_status(silver_dir, make_response)
    archive = RawArchive(silver_dir)
    archive.append(
        "station_information",
        make_response("station_information.json", fetched_at=NOW - timedelta(hours=2)),
        fingerprint="info",
    )
    archive.append(
        WEATHER_FEED,
        RawResponse(content=body(9), fetched_at=NOW - timedelta(minutes=10)),
        fingerprint="w",
    )
    return silver_dir


def test_predict_command_writes_the_document(served: Path) -> None:
    out = served / "serving" / "forecasts.json"

    assert (
        main(
            [
                "--data-dir",
                str(served),
                "--model",
                str(served / "models" / "lightgbm.pkl"),
                "--out",
                str(out),
            ],
            now=NOW,
        )
        == 0
    )

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["origin"] == "2026-11-12T08:00:00+00:00"
    assert document["model"]["name"] == "lightgbm"
    assert {"data_start", "data_end", "examples", "interval_level"} <= set(document["model"])
    station = document["stations"][0]
    assert set(station) == {
        "station_id",
        "code",
        "name",
        "lat",
        "lon",
        "capacity",
        "now",
        "forecasts",
    }
    forecast = station["forecasts"]["60"]
    assert 0.0 <= forecast["p_empty"] <= 1.0
    low, high = forecast["mechanical_interval"]
    assert low <= round(forecast["mechanical"]) <= high
    assert not out.with_suffix(".json.part").exists()
    status = document["status"]
    assert status["collector"]["fetches_last_hour"] == 6  # every line of the last hour
    assert status["weather"]["fetched_at"] == "2026-11-12T07:53:30+00:00"
    assert status["models"][0]["trained_at"] == document["model"]["trained_at"]
    assert (served / "serving" / "monitor" / "forecasts").is_dir()


def test_predict_command_without_recent_data_fails(served: Path) -> None:
    later = NOW + timedelta(hours=3)

    assert (
        main(
            [
                "--data-dir",
                str(served),
                "--model",
                str(served / "models" / "lightgbm.pkl"),
                "--out",
                str(served / "f.json"),
            ],
            now=later,
        )
        == 1
    )


def test_predict_command_can_publish(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uploads: list[tuple[bytes, str]] = []

    def fake_publish(payload: bytes, url: str) -> int:
        uploads.append((payload, url))
        return len(payload)

    monkeypatch.setattr(live, "publish", fake_publish)
    out = served / "f.json"

    code = main(
        [
            "--data-dir",
            str(served),
            "--model",
            str(served / "models" / "lightgbm.pkl"),
            "--out",
            str(out),
            "--publish",
            "gs://bucket/forecasts.json",
        ],
        now=NOW,
    )

    assert code == 0
    assert uploads == [(out.read_bytes(), "gs://bucket/forecasts.json")]


def test_predict_publishes_where_the_environment_says(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def fake_publish(payload: bytes, url: str) -> int:
        urls.append(url)
        return len(payload)

    monkeypatch.setattr(live, "publish", fake_publish)
    monkeypatch.setenv(live.PUBLISH_ENV, "gs://bucket/from-env.json")
    model = str(served / "models" / "lightgbm.pkl")

    assert (
        main(
            ["--data-dir", str(served), "--model", model, "--out", str(served / "f.json")], now=NOW
        )
        == 0
    )
    assert urls == ["gs://bucket/from-env.json"]


def test_a_monitoring_failure_does_not_stop_the_forecasts(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> dict[str, object]:
        raise OSError("disk full")

    monkeypatch.setattr(Monitor, "update", broken)
    out = served / "f.json"
    model = str(served / "models" / "lightgbm.pkl")

    assert main(["--data-dir", str(served), "--model", model, "--out", str(out)], now=NOW) == 0
    assert "status" not in json.loads(out.read_text(encoding="utf-8"))
