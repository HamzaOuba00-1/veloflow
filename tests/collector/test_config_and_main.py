"""Tests for settings validation and the entry point."""

import logging
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from veloflow.collector import __main__ as entry
from veloflow.collector.config import CollectorSettings
from veloflow.collector.service import Collector


def test_defaults_match_the_design(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env file
    settings = CollectorSettings()

    assert settings.poll_interval_s == 30.0
    assert settings.gbfs_url.endswith("/gbfs.json")
    assert settings.healthcheck_url is None


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VELOFLOW_COLLECTOR_DATA_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("VELOFLOW_COLLECTOR_POLL_INTERVAL_S", "60")

    settings = CollectorSettings()

    assert settings.data_dir == tmp_path / "store"
    assert settings.poll_interval_s == 60.0


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("VELOFLOW_COLLECTOR_POLL_INTERVAL_S", "5"),  # below the politeness floor
        ("VELOFLOW_COLLECTOR_TIMEOUT_S", "45"),  # longer than the polling interval
    ],
)
def test_invalid_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, value: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        CollectorSettings()


def test_main_wires_everything_and_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VELOFLOW_COLLECTOR_DATA_DIR", str(tmp_path))
    ran: list[bool] = []
    handlers: dict[int, Callable[..., None]] = {}
    logging_config: dict[str, Any] = {}
    monkeypatch.setattr(Collector, "run", lambda self: ran.append(True))
    # Keep the test process untouched: no real signal handlers, no global logging change.
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.update({signum: handler})
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: logging_config.update(kwargs))
    monkeypatch.setattr(logging.Formatter, "converter", time.localtime)
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", httpx_logger.level)

    assert entry.main() == 0
    assert ran == [True]
    assert httpx_logger.level == logging.WARNING  # request URLs stay out of the logs
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
    assert logging_config["level"] == "INFO"
    assert logging.Formatter.converter is time.gmtime


def test_main_creates_the_heartbeat_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VELOFLOW_COLLECTOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VELOFLOW_COLLECTOR_HEALTHCHECK_URL", "https://hc-ping.com/x")
    wired: list[object] = []
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(logging.Formatter, "converter", time.localtime)
    monkeypatch.setattr(Collector, "run", lambda self: wired.append(self._heartbeat))

    assert entry.main() == 0
    assert type(wired[0]).__name__ == "Heartbeat"
    assert "outages will go unnoticed" not in caplog.text


def test_stop_signal_stops_the_collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VELOFLOW_COLLECTOR_DATA_DIR", str(tmp_path))
    handlers: dict[int, Callable[..., None]] = {}
    stopped: list[bool] = []
    monkeypatch.setattr(
        signal, "signal", lambda signum, handler: handlers.update({signum: handler})
    )
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(logging.Formatter, "converter", time.localtime)
    monkeypatch.setattr(Collector, "stop", lambda self: stopped.append(True))

    def run_until_signal(self: Collector) -> None:
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(Collector, "run", run_until_signal)

    assert entry.main() == 0
    assert stopped == [True]
