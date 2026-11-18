"""Tests for the model registry (champion.json, versions, sync on the VM)."""

import json
from pathlib import Path
from typing import Any

from veloflow.models.registry import Registry, local_version, sync_champion, version_name


def stored(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lightgbm.pkl").write_bytes(b"model")
    (folder / "lightgbm.json").write_text("{}")
    return folder


def test_promote_keep_the_previous_and_record_every_decision(
    memory_storage: Any, tmp_path: Path
) -> None:
    registry = Registry(memory_storage, "gs://models")
    assert registry.pointer() is None

    registry.upload("v1", stored(tmp_path / "a"), {"decision": "promoted"})
    registry.decide({"version": "v1"}, promote="v1")
    registry.decide({"version": "v2", "decision": "rejected"}, promote=None)
    pointer = registry.decide({"version": "v3"}, promote="v3")

    assert (pointer["current"], pointer["previous"]) == ("v3", "v1")
    assert [e["version"] for e in pointer["history"]] == ["v1", "v2", "v3"]
    assert json.loads(memory_storage.objects["gs://models/champion.json"]) == pointer
    assert "gs://models/versions/v1/evaluation.json" in memory_storage.objects


def test_the_vm_downloads_a_new_champion_once(memory_storage: Any, tmp_path: Path) -> None:
    registry = Registry(memory_storage, "gs://models")
    local = stored(tmp_path / "vm")
    assert sync_champion(registry, local) is None  # no registry yet: the local model stays
    assert local_version(local) is None

    registry.upload("v1", stored(tmp_path / "a"), {})
    registry.decide({}, promote="v1")
    sync_champion(registry, local)
    assert local_version(local) == "v1"
    writes = len(memory_storage.writes)
    reads_before = dict(memory_storage.objects)
    sync_champion(registry, local)  # unchanged: nothing downloaded
    assert memory_storage.objects == reads_before
    assert len(memory_storage.writes) == writes
    assert not list(local.glob("*.part"))


def test_version_names_sort_by_time() -> None:
    from datetime import UTC, datetime

    early = version_name(datetime(2026, 11, 23, 3, 15, tzinfo=UTC))
    late = version_name(datetime(2026, 11, 30, 3, 15, tzinfo=UTC))
    assert early == "20261123T031500Z" < late
