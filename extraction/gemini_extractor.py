"""
GeminiExtractor — google-genai SDK, gemini-2.5-flash.

Implements the Extractor ABC with:
- Vision path: list[Path] images → inline bytes → Part.from_bytes
- Text path: str text appended to prompt
- Hybrid: both together (per-page mix handled upstream by router)
- response_mime_type="application/json", response_schema=schema, temperature=0
- Usage metadata captured and attached to returned dict under _usage
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel

from extraction.base import Extractor, ExtractionError
from prompts.extract_v1 import SYSTEM_INSTRUCTION, USER_PROMPT

logger = logging.getLogger(__name__)

_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".gif": "image/gif",
}


class GeminiExtractor(Extractor):
    """
    Extraction via Google Gemini (gemini-2.5-flash by default).

    Usage:
        extractor = GeminiExtractor(api_key=settings.gemini_api_key)
        result = extractor.extract(schema=ResumeExtractPayload, images=[path])
    """

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
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
        """
        Extract structured resume data.

        Returns a dict validated against `schema`, plus a `_usage` key with
        token counts:  {"prompt_tokens": int, "output_tokens": int, "total_tokens": int}
        """
        from google.genai import types

        if not images and not text:
            raise ValueError("At least one of images or text must be provided.")

        # --- build parts list ---
        parts: list[Any] = [types.Part.from_text(text=USER_PROMPT)]

        if images:
            for img_path in images:
                img_path = Path(img_path)
                mime = _MIME_MAP.get(img_path.suffix.lower(), "image/jpeg")
                raw = img_path.read_bytes()
                parts.append(types.Part.from_bytes(data=raw, mime_type=mime))
                logger.debug("Attached image %s (%d bytes, %s)", img_path.name, len(raw), mime)

        if text:
            parts.append(types.Part.from_text(text=f"RESUME TEXT:\n{text}"))

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=schema,
        )

        response = self._call_with_retry(parts, config)

        # --- parse response ---
        raw_text = response.text
        if not raw_text:
            raise ExtractionError("Gemini returned an empty response.")

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                f"Gemini response was not valid JSON: {exc}\nRaw: {raw_text[:500]}"
            ) from exc

        # --- attach usage metadata ---
        usage = response.usage_metadata
        data["_usage"] = {
            "prompt_tokens": usage.prompt_token_count if usage else None,
            "output_tokens": usage.candidates_token_count if usage else None,
            "total_tokens": usage.total_token_count if usage else None,
            "model": self._model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "Gemini extraction complete — tokens: %s prompt / %s output",
            data["_usage"]["prompt_tokens"],
            data["_usage"]["output_tokens"],
        )
        return data

    def _call_with_retry(self, parts: list, config: Any, max_retries: int = 4) -> Any:
        """Call generate_content with exponential backoff on 429/5xx."""
        import time
        from google.genai import errors as genai_errors

        delay = 5.0
        for attempt in range(1, max_retries + 1):
            try:
                return self._client.models.generate_content(
                    model=self._model,
                    contents=parts,
                    config=config,
                )
            except (genai_errors.ServerError, genai_errors.ClientError) as exc:
                # Retry on 5xx (ServerError) and 429 rate-limit (ClientError with status 429)
                is_retryable = isinstance(exc, genai_errors.ServerError) or \
                    (isinstance(exc, genai_errors.ClientError) and "429" in str(exc))
                if not is_retryable:
                    raise ExtractionError(
                        f"Gemini non-retryable error: {exc}", attempts=attempt
                    ) from exc
                if attempt == max_retries:
                    raise ExtractionError(
                        f"Gemini API failed after {attempt} attempts: {exc}",
                        attempts=attempt,
                    ) from exc
                logger.warning(
                    "Gemini %s (attempt %d/%d) — retrying in %.0fs …",
                    type(exc).__name__, attempt, max_retries, delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                raise ExtractionError(
                    f"Gemini API call failed: {exc}",
                    attempts=attempt,
                ) from exc
