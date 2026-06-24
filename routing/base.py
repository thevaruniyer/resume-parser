"""
Router — abstract interface for file-type classification and path selection.

Contract: connector → router → extractor → record → sink
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class ExtractionPath(str, Enum):
    TEXT = "text"       # native text layer (PDF text / DOCX)
    VISION = "vision"   # image-based LLM extraction
    HYBRID = "hybrid"   # per-page mix of text + vision
    FORM = "form"       # AcroForm / XFA field extraction
    DEAD_LETTER = "dead_letter"  # corrupt / encrypted / unreadable → flag


@dataclass
class RoutingDecision:
    path: ExtractionPath
    file_type: str                        # e.g. "pdf", "jpg", "docx"
    reason: str = ""                      # human-readable rationale
    page_paths: dict[int, ExtractionPath] = field(default_factory=dict)
    # per-page override for HYBRID; key = 0-indexed page number
    escalation_history: list[ExtractionPath] = field(default_factory=list)
    dead_letter_reason: Optional[str] = None


class Router(ABC):
    """
    Abstract router.

    Phase 2 will implement cascade logic:
    classify(path) → text | vision | hybrid | form | dead_letter
    with escalation loop on extraction failure.
    """

    @abstractmethod
    def classify(self, path: Path) -> RoutingDecision:
        """
        Inspect path and return a RoutingDecision.
        Must not call the extractor — pure classification only.
        """
