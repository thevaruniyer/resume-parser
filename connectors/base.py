"""
StorageConnector — abstract interface for all file sources.

Contract: connector → router → extractor → record → sink
New storage target = new connector only; no other layer changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class FileInfo:
    """Lightweight descriptor returned by list_files()."""
    name: str             # basename, e.g. "resume_01.pdf"
    path: str             # source-system path or URI
    file_type: str        # extension without dot, lowercased: "pdf", "jpg", …
    size_bytes: int = 0
    modified_at: str = ""  # ISO 8601 if available
    content_hash: str = ""  # sha256 hex if pre-computed by source
    extra: dict = field(default_factory=dict)


class StorageConnector(ABC):
    """
    Abstract base for all file-source connectors.

    Implementations:
    - LocalFolderConnector (Phase 5): walks a local directory
    - RcloneConnector (Phase 5): shells out to rclone for OneDrive/GDrive
    """

    @abstractmethod
    def list_files(self) -> Iterator[FileInfo]:
        """
        Yield FileInfo for every resume file in the source.
        Must be idempotent — calling twice yields the same set.
        """

    @abstractmethod
    def download(self, file_info: FileInfo, dest_dir: Path) -> Path:
        """
        Ensure file_info is available locally.
        Returns the local Path of the downloaded (or already-local) file.
        Must NOT modify the source; read-only access only.
        """
