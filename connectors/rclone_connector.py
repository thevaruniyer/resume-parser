"""
RcloneConnector — reads resumes from any rclone-supported remote.

All rclone interaction is via subprocess only (no Azure/Microsoft SDK).
This connector is used for testing against real OneDrive.
Production deployments will use the Graph API directly (Phase 8).

Configuration (from env / config.py):
    RCLONE_REMOTE — rclone remote name, e.g. "onedrive"
    RCLONE_PATH   — path on the remote, e.g. "ResumeTest/"

Manifest: output_data/manifests/rclone_<remote>_<sha1(path)[:8]>.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from config import settings
from connectors.base import (
    ConfigError,
    FileRecord,
    StorageConnector,
    _read_manifest,
    _write_manifest_atomic,
)

logger = logging.getLogger(__name__)

_MANIFEST_DIR = settings.output_dir / "manifests"
_TMP_DIR = settings.output_dir / "tmp"

_SUPPORTED_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp",
    ".tiff", ".tif", ".heic", ".gif",
})


def _path_key(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:8]


def _pick_hash(hashes: dict) -> str:
    """Return SHA-1, then MD5, then first available hash value."""
    for key in ("SHA-1", "MD5"):
        if hashes.get(key):
            return hashes[key]
    for v in hashes.values():
        if v:
            return v
    return ""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )


class RcloneConnector(StorageConnector):
    """
    Connector that lists and downloads files via rclone subprocess calls.

    Args:
        remote: rclone remote name (default: settings.rclone_remote)
        path:   path on the remote  (default: settings.rclone_path)
    """

    def __init__(
        self,
        remote: str | None = None,
        path: str | None = None,
    ) -> None:
        # If not explicitly passed, fall back to env; empty string counts as "not set"
        self._remote = remote if remote is not None else (settings.rclone_remote or "")
        self._path = path if path is not None else (settings.rclone_path or "")
        if not self._remote:
            raise ConfigError(
                "RCLONE_REMOTE is not set. Add it to .env, e.g. RCLONE_REMOTE=onedrive"
            )
        if not self._path:
            raise ConfigError(
                "RCLONE_PATH is not set. Add it to .env, e.g. RCLONE_PATH=ResumeTest/"
            )
        key = _path_key(self._path)
        self._manifest_path = _MANIFEST_DIR / f"rclone_{self._remote}_{key}.json"

    # ------------------------------------------------------------------
    # StorageConnector interface
    # ------------------------------------------------------------------

    def list_files(self) -> list[FileRecord]:
        """Run rclone lsjson --hash and return one FileRecord per non-dir file."""
        remote_root = f"{self._remote}:{self._path}"
        result = _run(["rclone", "lsjson", "--hash", remote_root])
        if result.returncode != 0:
            raise RuntimeError(
                f"rclone lsjson failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"rclone lsjson returned invalid JSON: {exc}") from exc

        records: list[FileRecord] = []
        for entry in entries:
            if entry.get("IsDir"):
                continue
            name: str = entry.get("Name", "")
            ext = Path(name).suffix.lower()
            if ext not in _SUPPORTED_EXTS:
                continue
            hashes: dict = entry.get("Hashes") or {}
            records.append(FileRecord(
                name=name,
                path=f"{self._path.rstrip('/')}/{name}",
                file_type=ext.lstrip("."),
                size=entry.get("Size", 0),
                modified_at=entry.get("ModTime", ""),
                file_hash=_pick_hash(hashes),
            ))
        return records

    def download(self, file_record: FileRecord) -> Path:
        """Download one file from the remote to output_data/tmp/."""
        _TMP_DIR.mkdir(parents=True, exist_ok=True)
        local_path = _TMP_DIR / file_record.name
        remote_src = f"{self._remote}:{self._path.rstrip('/')}/{file_record.name}"
        result = _run(["rclone", "copyto", remote_src, str(local_path)])
        if result.returncode != 0:
            raise RuntimeError(
                f"rclone copyto failed for {file_record.name} "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )
        return local_path

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        _write_manifest_atomic(self._manifest_path, manifest)

    def load_manifest(self) -> dict[str, Any]:
        return _read_manifest(self._manifest_path)

    def cleanup_downloaded(self, local_path: Path) -> None:
        """Remove the tmp file after processing."""
        try:
            local_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete tmp file %s: %s", local_path, exc)
