"""
LocalFolderConnector — reads resumes from a local directory.

Used for development/testing (Phase 5).  Production uses RcloneConnector.
Files are already local so download() is a no-op path return.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from config import settings
from connectors.base import (
    FileRecord,
    StorageConnector,
    _read_manifest,
    _write_manifest_atomic,
)

_SUPPORTED_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp",
    ".tiff", ".tif", ".heic", ".gif",
})

_MANIFEST_DIR = settings.output_dir / "manifests"


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _folder_key(folder: Path) -> str:
    return hashlib.sha1(str(folder.resolve()).encode()).hexdigest()[:8]


class LocalFolderConnector(StorageConnector):
    """
    Scans a local folder for resume files.

    Manifest: output_data/manifests/local_<folder_hash>.json
    """

    def __init__(self, folder: Path | str) -> None:
        self._folder = Path(folder).resolve()
        self._manifest_path = _MANIFEST_DIR / f"local_{_folder_key(self._folder)}.json"

    # ------------------------------------------------------------------
    # StorageConnector interface
    # ------------------------------------------------------------------

    def list_files(self) -> list[FileRecord]:
        records: list[FileRecord] = []
        for entry in sorted(self._folder.iterdir(), key=lambda e: e.name):
            if not entry.is_file():
                continue
            if entry.name.startswith("."):
                continue
            ext = entry.suffix.lower()
            if ext not in _SUPPORTED_EXTS:
                continue
            stat = entry.stat()
            records.append(FileRecord(
                name=entry.name,
                path=str(entry),
                file_type=ext.lstrip("."),
                size=stat.st_size,
                modified_at="",
                file_hash=_sha256(entry),
            ))
        return records

    def download(self, file_record: FileRecord) -> Path:
        return Path(file_record.path)

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        _write_manifest_atomic(self._manifest_path, manifest)

    def load_manifest(self) -> dict[str, Any]:
        return _read_manifest(self._manifest_path)

    # cleanup_downloaded: inherited no-op (file is already local)
