"""
Sink — abstract interface for all output destinations.

Contract: connector → router → extractor → record → sink
New output format = new sink only; no other layer changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from schema import ResumeRecord


class Sink(ABC):
    """
    Abstract base for all output sinks.

    Implementations (Phase 4+):
    - ExcelSink: openpyxl-based upsert into the client's workbook.
    """

    @abstractmethod
    def write(self, record: ResumeRecord) -> None:
        """
        Persist a parsed record to the destination.

        Rules:
        - MUST be idempotent: writing the same record twice = no-op.
        - Records with needs_review=True → review sheet, not main sheet.
        - Never write a row from a failed or flagged parse without marking it.
        """

    @abstractmethod
    def close(self) -> None:
        """Flush and release any held resources (file handles, connections)."""
