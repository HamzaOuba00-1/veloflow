"""Model registry in Cloud Storage: versions, the champion in service, history.

Layout under the registry URL (``gs://veloflow-models-513808``)::

    versions/<version>/lightgbm.pkl     the model (pickle written by save_model)
    versions/<version>/lightgbm.json    its card
    versions/<version>/evaluation.json  champion against challenger, decision
    champion.json                       {"current", "previous", "history": [...]}

``champion.json`` is the only object that changes what is in service:
promoting a version or rolling back is one write of that file. The collector
VM reads it before each forecast run (``sync_champion``) and downloads a new
version once.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from veloflow.gcs import Storage, join
from veloflow.models.artifact import MODEL_NAME

logger = logging.getLogger(__name__)

CHAMPION = "champion.json"
VERSION_FILE = "VERSION"  # beside the local model: the version it is
FILES = (f"{MODEL_NAME}.pkl", f"{MODEL_NAME}.json")
HISTORY_KEPT = 30  # decisions remembered in champion.json


def version_name(moment: datetime) -> str:
    """A sortable version name: ``20261123T031502Z``."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class Registry:
    """The registry at ``base`` (``gs://bucket`` or ``gs://bucket/prefix``)."""

    storage: Storage
    base: str

    def pointer(self) -> dict[str, Any] | None:
        """The content of ``champion.json``, or ``None`` before the first version."""
        raw = self.storage.read(join(self.base, CHAMPION))
        return None if raw is None else dict(json.loads(raw))

    def upload(self, version: str, folder: Path, evaluation: dict[str, Any]) -> None:
        """Store the model files of ``folder`` and the evaluation under ``version``."""
        for name in FILES:
            self.storage.write(
                join(self.base, "versions", version, name), (folder / name).read_bytes()
            )
        self.record_evaluation(version, evaluation)

    def record_evaluation(self, version: str, evaluation: dict[str, Any]) -> None:
        """Store the evaluation of a version (also of a challenger left out)."""
        body = json.dumps(evaluation, indent=2).encode()
        self.storage.write(
            join(self.base, "versions", version, "evaluation.json"), body, "application/json"
        )

    def decide(self, entry: dict[str, Any], *, promote: str | None) -> dict[str, Any]:
        """Add ``entry`` to the history; with ``promote``, put that version in service.

        Returns:
            The new content of ``champion.json``.
        """
        pointer = self.pointer() or {"current": None, "previous": None, "history": []}
        if promote is not None:
            pointer["previous"], pointer["current"] = pointer.get("current"), promote
        pointer["history"] = [*pointer.get("history", []), entry][-HISTORY_KEPT:]
        body = json.dumps(pointer, indent=2).encode()
        self.storage.write(join(self.base, CHAMPION), body, "application/json")
        return pointer

    def download(self, version: str, folder: Path) -> Path:
        """Write the files of ``version`` into ``folder``; return the pickle path.

        Each file lands under a temporary name first, the pickle last, then
        ``VERSION``: a reader never loads half a model or a mismatched card.
        """
        folder.mkdir(parents=True, exist_ok=True)
        for name in reversed(FILES):  # the card first, the model last
            data = self.storage.read(join(self.base, "versions", version, name))
            if data is None:
                raise FileNotFoundError(f"version {version} has no {name}")
            partial = folder / f"{name}.part"
            partial.write_bytes(data)
            partial.replace(folder / name)
        (folder / VERSION_FILE).write_text(version, encoding="utf-8")
        return folder / FILES[0]


def local_version(folder: Path) -> str | None:
    """The version of the model in ``folder``, if it came from the registry."""
    path = folder / VERSION_FILE
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def sync_champion(registry: Registry, folder: Path) -> dict[str, Any] | None:
    """Download the champion into ``folder`` when it changed; return ``champion.json``.

    A registry without a champion leaves the local model alone (the model
    copied by hand stays in service until the first promotion).
    """
    pointer = registry.pointer()
    current = pointer.get("current") if pointer else None
    if current and current != local_version(folder):
        registry.download(current, folder)
        logger.info("champion %s downloaded to %s", current, folder)
    return pointer
