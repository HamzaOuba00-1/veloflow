"""Tests for sharing the recent data with the training job."""

from datetime import date
from pathlib import Path
from typing import Any

from veloflow.storage.share import main, share, shared_files

TODAY = date(2026, 11, 18)


def day_files(root: Path, day: str, size: int) -> None:
    silver = root / "silver" / "station_status" / f"date={day}"
    silver.mkdir(parents=True)
    (silver / "station_status.parquet").write_bytes(b"x" * size)
    raw = root / "raw" / "station_information"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"station_information_{day}.jsonl.gz").write_bytes(b"y")


def test_only_complete_recent_days_are_shared(tmp_path: Path) -> None:
    for day in ("2026-11-10", "2026-11-11", "2026-11-17", "2026-11-18"):
        day_files(tmp_path, day, 3)
    names = [p.relative_to(tmp_path).as_posix() for p in shared_files(tmp_path, TODAY, 7)]
    # 18 November is today (not complete); 10 November is more than 7 days ago.
    assert names == [
        "silver/station_status/date=2026-11-11/station_status.parquet",
        "silver/station_status/date=2026-11-17/station_status.parquet",
        "raw/station_information/station_information_2026-11-11.jsonl.gz",
        "raw/station_information/station_information_2026-11-17.jsonl.gz",
    ]


def test_a_second_run_sends_nothing(tmp_path: Path, memory_storage: Any) -> None:
    day_files(tmp_path, "2026-11-17", 3)
    assert share(memory_storage, tmp_path, "gs://data", TODAY) == 2
    assert "gs://data/silver/station_status/date=2026-11-17/station_status.parquet" in (
        memory_storage.objects
    )
    assert share(memory_storage, tmp_path, "gs://data", TODAY) == 0
    (tmp_path / "silver/station_status/date=2026-11-17/station_status.parquet").write_bytes(b"zz")
    assert share(memory_storage, tmp_path, "gs://data", TODAY) == 1  # recompacted: sent again


def test_without_a_bucket_nothing_happens(tmp_path: Path, memory_storage: Any) -> None:
    assert main(["--data-dir", str(tmp_path)], storage=memory_storage) == 0
    assert memory_storage.writes == []
