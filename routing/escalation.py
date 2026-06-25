"""
Escalation loop — wraps extraction with per-tier scoring and fallback.

Pipeline step after routing:
  1. Classify file → RoutingDecision (text | vision | hybrid | dead_letter)
  2. Extract with the classified path
  3. Score the result (schema-valid? key fields present?)
  4. If score fails → escalate: text→hybrid→vision, retry once
  5. If still fails → flag needs_review=True, return best partial result

This module owns the orchestration; the extractor and router are injected.
"""
from __future__ import annotations

import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Type

from pydantic import BaseModel, ValidationError

from extraction.base import Extractor, ExtractionError
from routing.base import ExtractionPath, RoutingDecision

logger = logging.getLogger(__name__)

# Ordered escalation ladder: each tier is more expensive than the previous
_ESCALATION_LADDER = [ExtractionPath.TEXT, ExtractionPath.HYBRID, ExtractionPath.VISION]


def extract_with_escalation(
    path: Path,
    router,              # FileRouter or any Router
    extractor: Extractor,
    schema: Type[BaseModel],
    *,
    confidence_threshold: float = 0.70,
) -> tuple[Optional[dict[str, Any]], RoutingDecision]:
    """
    Classify, extract, score, and escalate as needed.

    Returns:
        (raw_dict | None, final_RoutingDecision)
        raw_dict is None only when the file is dead-lettered before extraction.
        RoutingDecision.needs_review is True when escalation was exhausted or
        extraction partially failed.
    """
    decision = router.classify(path)

    if decision.path == ExtractionPath.DEAD_LETTER:
        logger.info("Dead-lettered: %s — %s", path.name, decision.dead_letter_reason)
        return None, decision

    # Build the escalation sequence starting from the classified path
    start_idx = _ESCALATION_LADDER.index(decision.path) if decision.path in _ESCALATION_LADDER else 2
    tiers = _ESCALATION_LADDER[start_idx:]

    best_raw: Optional[dict] = None
    review_reasons: list[str] = []

    for tier in tiers:
        try:
            raw = _extract_for_tier(path, tier, extractor, schema, decision)
        except ExtractionError as exc:
            logger.warning("Extraction failed on %s path for %s: %s", tier, path.name, exc)
            review_reasons.append(f"extraction_failed_{tier}:{exc}")
            decision.escalation_history.append(tier)
            continue

        # Score the result
        score_ok, score_reason = _score(raw, schema)
        if best_raw is None:
            best_raw = raw

        if score_ok:
            logger.info("Extraction succeeded on %s path for %s", tier, path.name)
            decision = RoutingDecision(
                path=tier,
                file_type=decision.file_type,
                reason=decision.reason,
                page_paths=decision.page_paths,
                escalation_history=decision.escalation_history,
            )
            return raw, decision

        logger.info(
            "Score failed on %s path for %s (%s) — escalating …",
            tier, path.name, score_reason,
        )
        review_reasons.append(f"score_fail_{tier}:{score_reason}")
        decision.escalation_history.append(tier)
        best_raw = raw  # keep the last result as best effort

    # Exhausted all tiers
    logger.warning("All extraction tiers exhausted for %s", path.name)
    review_reasons.append("escalation_exhausted")
    decision = RoutingDecision(
        path=decision.escalation_history[-1] if decision.escalation_history else ExtractionPath.VISION,
        file_type=decision.file_type,
        reason=decision.reason + "|escalation_exhausted",
        page_paths=decision.page_paths,
        escalation_history=decision.escalation_history,
        dead_letter_reason=None,
    )
    if best_raw is not None:
        best_raw.setdefault("_review_reasons", []).extend(review_reasons)
    return best_raw, decision


def _extract_for_tier(
    path: Path,
    tier: ExtractionPath,
    extractor: Extractor,
    schema: Type[BaseModel],
    decision: RoutingDecision,
) -> dict[str, Any]:
    """Extract using the given tier's strategy."""
    if tier == ExtractionPath.VISION:
        image_paths = _get_image_paths(path)
        if not image_paths:
            raise ExtractionError(
                f"no_images_for_vision: {path.name} has no renderable images"
            )
        return extractor.extract(schema=schema, images=image_paths)

    elif tier == ExtractionPath.TEXT:
        text = _extract_text(path, decision)
        return extractor.extract(schema=schema, text=text)

    elif tier == ExtractionPath.HYBRID:
        # Hybrid: send text pages as text, image pages as images
        text = _extract_text(path, decision)
        image_paths = _get_image_paths(path)
        # For simplicity in Phase 2, send both; the model decides what to use
        return extractor.extract(schema=schema, images=image_paths, text=text)

    raise ValueError(f"Unknown extraction tier: {tier}")


def _extract_text(path: Path, decision: RoutingDecision) -> str:
    """Extract text from the file according to its type."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif ext in {".docx"}:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    elif ext == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    else:
        return ""


def _get_image_paths(path: Path) -> list[Path]:
    """Return image paths for the given file (render PDF pages if needed)."""
    ext = path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".gif"}

    if ext in image_exts:
        return [path]
    elif ext == ".pdf":
        from routing.pdf_utils import pdf_to_image_paths
        return pdf_to_image_paths(path, dpi=150)
    else:
        return []


def _score(raw: dict, schema: Type[BaseModel]) -> tuple[bool, str]:
    """
    Quick quality score — passes if:
    1. dict is schema-valid (no ValidationError on the extraction fields)
    2. full_name is present and non-null
    """
    # Schema validity (ignore _usage / internal keys)
    data = {k: v for k, v in raw.items() if not k.startswith("_")}
    try:
        schema.model_validate(data)
    except ValidationError as exc:
        return False, f"schema_invalid:{exc.error_count()} errors"

    # Key-field presence
    if not raw.get("full_name"):
        return False, "full_name_null"

    return True, "ok"
