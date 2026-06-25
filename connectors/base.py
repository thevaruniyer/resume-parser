"""
StorageConnector — abstract interface for all file-source connectors.

Contract: connector → router → extractor → record → sink
New storage target = new connector only; no other layer changes.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FileRecord:
    """Lightweight descriptor returned by list_files()."""
    name: str           # bare filename, e.g. "resume_01.pdf"
    path: str           # source-system path or URI
    file_type: str      # extension without dot, lowercased: "pdf", "jpg", …
    size: int = 0
    modified_at: str = ""   # ISO 8601 from source, informational
    file_hash: str = ""     # hash hex for delta detection (SHA-1/SHA-256/MD5)


class ConfigError(Exception):
    """Raised when a connector is missing required configuration."""


class StorageConnector(ABC):
    """
    Abstract base for all file-source connectors.

    Subclasses implement list_files(), download(), save_manifest(),
    and load_manifest().  delta() has a default implementation that
    compares file_hash values — override only if source provides its own
    efficient change-detection API.
    """

    # ------------------------------------------------------------------
    # Core interface (subclasses must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    def list_files(self) -> list[FileRecord]:
        """
        Return FileRecord for every resume file in the source.
        Idempotent — calling twice returns the same set.
        """

    @abstractmethod
    def download(self, file_record: FileRecord) -> Path:
        """
        Ensure file_record is available locally.
        Returns the local Path of the file.
        Must NOT modify the source (read-only).
        """

    @abstractmethod
    def save_manifest(self, manifest: dict[str, Any]) -> None:
        """Persist the manifest JSON atomically."""

    @abstractmethod
    def load_manifest(self) -> dict[str, Any]:
        """Load manifest JSON; return {} if it does not exist."""

    # ------------------------------------------------------------------
    # Default implementations
    # ------------------------------------------------------------------

    def delta(self, manifest: dict[str, Any]) -> list[FileRecord]:
        """
        Return files that are new or whose hash changed since the manifest
        was last saved.  Files in the manifest that no longer exist in the
        source are silently ignored (no row deletion).
        """
        changed: list[FileRecord] = []
        for fr in self.list_files():
            stored = manifest.get(fr.name, {})
            if stored.get("hash") != fr.file_hash:
                changed.append(fr)
        return changed

    def cleanup_downloaded(self, local_path: Path) -> None:
        """Remove a locally-downloaded file.  No-op for local connectors."""


# ---------------------------------------------------------------------------
# Manifest I/O helpers (shared by all connectors)
# ---------------------------------------------------------------------------

def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    """Write manifest JSON atomically (tmp sibling → replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
