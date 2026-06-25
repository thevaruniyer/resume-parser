"""
OpenAICompatExtractor — works with any OpenAI-compatible inference endpoint.

Supports:
- GitHub Models  (base_url=https://models.inference.ai.azure.com, model=gpt-4o)
- OpenRouter     (base_url=https://openrouter.ai/api/v1, model=google/gemini-2.0-flash-exp:free)

Vision is handled via base64 image_url content blocks (standard OpenAI chat completions API).
Text is passed as a text content block appended after the user prompt.
"""
from __future__ import annotations

import base64
import json
import logging
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


class OpenAICompatExtractor(Extractor):
    """
    Extraction via any OpenAI-compatible chat completions endpoint.

    Args:
        api_key:       API key for the endpoint.
        model:         Model identifier string (e.g. "gpt-4o").
        base_url:      Full base URL of the endpoint.
        extra_headers: Additional HTTP headers (e.g. HTTP-Referer for OpenRouter).
        model_label:   Human-readable label for meta.model_used (defaults to model).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        extra_headers: Optional[dict[str, str]] = None,
        model_label: Optional[str] = None,
    ) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers or {},
            max_retries=0,  # FallbackExtractor handles retry logic
        )
        self._model = model
        self._label = model_label or model

    @property
    def model_name(self) -> str:
        return self._label

    def extract(
        self,
        schema: Type[BaseModel],
        images: Optional[list[Path]] = None,
        text: Optional[str] = None,
    ) -> dict[str, Any]:
        import openai

        if not images and not text:
            raise ValueError("At least one of images or text must be provided.")

        schema_hint = json.dumps(schema.model_json_schema(), indent=2)
        system_msg = (
            SYSTEM_INSTRUCTION
            + "\n\nRespond with ONLY a single valid JSON object — no markdown fences, "
            "no explanation, no extra keys. The JSON must match this schema:\n"
            f"```json\n{schema_hint}\n```"
        )

        content: list[dict] = [{"type": "text", "text": USER_PROMPT}]

        if images:
            for img_path in images:
                img_path = Path(img_path)
                mime = _MIME_MAP.get(img_path.suffix.lower(), "image/jpeg")
                b64 = base64.b64encode(img_path.read_bytes()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
                logger.debug("Attached image %s (%s)", img_path.name, mime)

        if text:
            content.append({"type": "text", "text": f"RESUME TEXT:\n{text}"})

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": content},
        ]

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=8192,
            )
        except openai.RateLimitError as exc:
            raise ExtractionError(
                f"429_rate_limit on {self._label}: {exc}"
            ) from exc
        except openai.APIStatusError as exc:
            if exc.status_code == 429:
                raise ExtractionError(
                    f"429_rate_limit on {self._label}: {exc}"
                ) from exc
            raise ExtractionError(
                f"api_error on {self._label} (HTTP {exc.status_code}): {exc.message}"
            ) from exc
        except openai.APIConnectionError as exc:
            raise ExtractionError(
                f"connection_error on {self._label}: {exc}"
            ) from exc

        raw_text = response.choices[0].message.content if response.choices else ""
        if not raw_text:
            raise ExtractionError(f"{self._label} returned an empty response.")

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                f"{self._label} response was not valid JSON: {exc}\nRaw: {raw_text[:500]}"
            ) from exc

        usage = response.usage
        data["_usage"] = {
            "prompt_tokens": usage.prompt_tokens if usage else None,
            "output_tokens": usage.completion_tokens if usage else None,
            "total_tokens": usage.total_tokens if usage else None,
            "model": self._label,
        }

        logger.info(
            "%s extraction complete — tokens: %s prompt / %s output",
            self._label,
            data["_usage"]["prompt_tokens"],
            data["_usage"]["output_tokens"],
        )
        return data
