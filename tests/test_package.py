"""Smoke tests for the installed ``veloflow`` package."""

import importlib
import importlib.metadata
from collections.abc import Iterator

import pytest

import veloflow


def test_version_matches_installed_metadata() -> None:
    """The exposed version comes from the installed package metadata."""
    assert veloflow.__version__ == importlib.metadata.version("veloflow")


@pytest.fixture
def restore_veloflow(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Undo patches, then re-import ``veloflow`` so other tests see the real module."""
    yield
    monkeypatch.undo()
    importlib.reload(veloflow)


@pytest.mark.usefixtures("restore_veloflow")
def test_version_falls_back_when_package_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing from a source tree that was never installed must not crash."""

    def raise_not_found(distribution_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(distribution_name)

    # The version lookup runs at import time, so we patch it and re-execute the module.
    monkeypatch.setattr(importlib.metadata, "version", raise_not_found)
    importlib.reload(veloflow)

    assert veloflow.__version__ == "0.0.0"
