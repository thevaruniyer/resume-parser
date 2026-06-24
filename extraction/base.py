"""
Extractor — model-agnostic abstract interface.

Contract: connector → router → extractor → record → sink

Implementations (Phase 1+):
- GeminiExtractor: google-genai SDK, gemini-2.5-flash
- QwenExtractor: DashScope OpenAI-compatible endpoint, qwen-vl-plus
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel


class Extractor(ABC):
    """
    Model-agnostic extractor interface.

    extract() accepts either images OR text (or both for hybrid),
    validates against schema, and returns a raw dict.
    The caller is responsible for constructing the ResumeRecord.
    """

    @abstractmethod
    def extract(
        self,
        schema: Type[BaseModel],
        images: Optional[list[Path]] = None,
        text: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Extract structured data from a resume.

        Args:
            schema: Pydantic model class that defines the output shape.
            images: List of image paths (for vision path). May be None.
            text:   Pre-extracted text (for text path). May be None.

        Returns:
            Raw dict that should be schema-valid against `schema`.

        Raises:
            ExtractionError: on model API failure or invalid JSON response.
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Human-readable model identifier, e.g. 'gemini-2.5-flash'."""


class ExtractionError(Exception):
    """Raised when extraction fails after all retries."""

    def __init__(self, message: str, source_file: str = "", attempts: int = 0):
        super().__init__(message)
        self.source_file = source_file
        self.attempts = attempts
