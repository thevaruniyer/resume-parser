"""
QwenExtractor stub — DashScope OpenAI-compatible endpoint, qwen-vl-plus.

Not implemented in Phase 1. Raises NotImplementedError on any call.
Will be implemented in Phase 1 extension or Phase 7 (hardening) once
DASHSCOPE_API_KEY is configured and model tier is confirmed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel

from extraction.base import Extractor


class QwenExtractor(Extractor):
    """
    Qwen-VL extractor stub.

    Satisfies the Extractor ABC so the module is importable, but all
    methods raise NotImplementedError until Phase 1 extension.
    """

    def __init__(self, api_key: str, model: str = "qwen-vl-plus") -> None:
        self._api_key = api_key
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    def extract(
        self,
        schema: Type[BaseModel],
        images: Optional[list[Path]] = None,
        text: Optional[str] = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "QwenExtractor is not yet implemented. "
            "Set DASHSCOPE_API_KEY and implement in a future phase."
        )
