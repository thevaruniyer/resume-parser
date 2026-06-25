"""
FallbackExtractor — tries extractors in order, moving to the next on quota exhaustion.

Fallback chain:
    Primary    → GeminiExtractor (GEMINI_API_KEY)
    Fallback 1 → GitHub Models GPT-4o (GITHUB_TOKEN)
    Fallback 2 → OpenRouter google/gemini-2.0-flash-exp:free (OPENROUTER_API_KEY)

Trigger rules:
    - 429 / quota / rate_limit error → try next extractor
    - Any other error               → dead-letter immediately (no further fallback)
    - All extractors exhausted      → raise ExtractionError("all_models_quota_exceeded: ...")

meta.model_used reflects whichever extractor actually succeeded.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel

from extraction.base import Extractor, ExtractionError

logger = logging.getLogger(__name__)

_QUOTA_KEYWORDS = ("429", "rate_limit", "quota", "resource_exhausted", "429_rate_limit")


def _is_quota_error(exc: ExtractionError) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _QUOTA_KEYWORDS)


class FallbackExtractor(Extractor):
    """
    Wrapper that runs a list of extractors in priority order.
    On quota/rate-limit, advances to the next; on other errors, re-raises immediately.
    """

    def __init__(self, extractors: list[Extractor]) -> None:
        if not extractors:
            raise ValueError("FallbackExtractor requires at least one extractor.")
        self._extractors = extractors
        self._last_model: str = extractors[0].model_name

    @property
    def model_name(self) -> str:
        return self._last_model

    def extract(
        self,
        schema: Type[BaseModel],
        images: Optional[list[Path]] = None,
        text: Optional[str] = None,
    ) -> dict[str, Any]:
        last_quota_exc: Optional[ExtractionError] = None

        for extractor in self._extractors:
            try:
                result = extractor.extract(schema=schema, images=images, text=text)
                self._last_model = extractor.model_name
                return result
            except ExtractionError as exc:
                if _is_quota_error(exc):
                    last_quota_exc = exc
                    logger.warning(
                        "Quota/rate-limit on %s — trying next fallback. (%s)",
                        extractor.model_name, exc,
                    )
                    continue
                # Non-quota error: surface immediately for dead-lettering
                raise

        raise ExtractionError(
            f"all_models_quota_exceeded: {last_quota_exc}",
            attempts=len(self._extractors),
        )


def build_fallback_extractor() -> FallbackExtractor:
    """
    Build the FallbackExtractor from available API keys in settings.
    Skips any tier whose key is missing.
    """
    from config import settings
    from extraction.gemini_extractor import GeminiExtractor
    from extraction.openai_compat_extractor import OpenAICompatExtractor

    extractors: list[Extractor] = [
        GeminiExtractor(api_key=settings.gemini_api_key),
    ]

    if settings.github_token:
        extractors.append(OpenAICompatExtractor(
            api_key=settings.github_token,
            model="gpt-4o",
            base_url="https://models.inference.ai.azure.com",
            model_label="github/gpt-4o",
        ))
        logger.debug("FallbackExtractor: GitHub Models GPT-4o added as fallback 1")
    else:
        logger.debug("FallbackExtractor: GITHUB_TOKEN not set — skipping GitHub fallback")

    if settings.openrouter_api_key:
        extractors.append(OpenAICompatExtractor(
            api_key=settings.openrouter_api_key,
            model="google/gemini-2.0-flash-exp:free",
            base_url="https://openrouter.ai/api/v1",
            extra_headers={
                "HTTP-Referer": "https://github.com/resume-parser",
                "X-Title": "resume-parser",
            },
            model_label="openrouter/google/gemini-2.0-flash-exp:free",
        ))
        logger.debug("FallbackExtractor: OpenRouter Gemini flash added as fallback 2")
    else:
        logger.debug("FallbackExtractor: OPENROUTER_API_KEY not set — skipping OpenRouter fallback")

    return FallbackExtractor(extractors)
