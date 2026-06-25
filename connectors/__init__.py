from connectors.base import ConfigError, FileRecord, StorageConnector
from connectors.local_connector import LocalFolderConnector
from connectors.rclone_connector import RcloneConnector

# Backward-compat alias (Phase 0 tests import FileInfo)
FileInfo = FileRecord

__all__ = [
    "StorageConnector",
    "FileRecord",
    "FileInfo",
    "ConfigError",
    "LocalFolderConnector",
    "RcloneConnector",
]
