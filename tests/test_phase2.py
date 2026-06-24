"""
Phase 2 tests — Multi-format routing cascade.

DoD:
  [x] Every corpus format routes to the correct ExtractionPath
  [x] image-only scanned PDF → VISION (no text layer)
  [x] garbled-text-layer PDF → VISION (quality probe fails)
  [x] two-column PDF → VISION (multicolumn detected)
  [x] screenshot JPG → VISION (image extension)
  [x] legacy .doc without LibreOffice → DEAD_LETTER
  [x] zero-byte corrupt PDF → DEAD_LETTER
  [x] text PDF (.pdf with real text layer) → TEXT
  [x] DOCX with sufficient text → TEXT
  [x] plain .txt → TEXT
  [x] escalation loop: mock extractor that fails TEXT → tries HYBRID → VISION
  [x] dead-lettered file: extract_with_escalation returns (None, decision)
  [x] DEAD_LETTER path skips extraction entirely (no extractor calls)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
CORPUS = ROOT / "test_corpus" / "files"
GT_DIR = ROOT / "ground_truth"

import sys
sys.path.insert(0, str(ROOT))

from routing import ExtractionPath, FileRouter, RoutingDecision, extract_with_escalation
from routing.text_quality import text_quality_ok
from schema import ResumeExtractPayload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gt(filename: str) -> dict:
    gt_file = GT_DIR / (Path(filename).stem + ".json")
    with open(gt_file) as f:
        return json.load(f)


def _router() -> FileRouter:
    return FileRouter()


# ---------------------------------------------------------------------------
# TextQualityResult unit tests
# ---------------------------------------------------------------------------

class TestTextQuality:
    def test_good_text_passes(self):
        text = "John Smith is a Chartered Accountant at ICAI with experience in audit " * 5
        result = text_quality_ok(text)
        assert result.ok is True

    def test_empty_text_fails(self):
        result = text_quality_ok("")
        assert result.ok is False
        assert result.reason  # any non-empty reason is fine

    def test_few_tokens_fails(self):
        result = text_quality_ok("hello world")
        assert result.ok is False
        assert "token" in result.reason

    def test_no_spaces_fails(self):
        # garbled: lots of chars, no spaces
        text = "abcdefghijklmnopqrstuvwxyz" * 20
        result = text_quality_ok(text)
        assert result.ok is False

    def test_high_junk_ratio_fails(self):
        # >30% non-ASCII, non-alnum chars
        junk = "́̂̃" * 50
        text = junk + " hello " * 5
        result = text_quality_ok(text)
        assert result.ok is False


# ---------------------------------------------------------------------------
# Per-fixture routing assertions
# ---------------------------------------------------------------------------

class TestRoutingScannedPdf:
    """Image-only PDF must route to VISION."""

    def test_path_is_vision(self):
        gt = _gt("sample_scanned.pdf")
        d = _router().classify(CORPUS / "sample_scanned.pdf")
        assert d.path == ExtractionPath.VISION
        assert not d.dead_letter_reason

    def test_reason_contains_image_only(self):
        gt = _gt("sample_scanned.pdf")
        d = _router().classify(CORPUS / "sample_scanned.pdf")
        assert gt["expected_routing_reason_contains"] in d.reason


class TestRoutingGarbledPdf:
    """PDF with garbage text layer must route to VISION via quality probe."""

    def test_path_is_vision(self):
        d = _router().classify(CORPUS / "sample_garbled.pdf")
        assert d.path == ExtractionPath.VISION

    def test_reason_indicates_garbled(self):
        gt = _gt("sample_garbled.pdf")
        d = _router().classify(CORPUS / "sample_garbled.pdf")
        assert gt["expected_routing_reason_contains"] in d.reason


class TestRoutingMulticolumnPdf:
    """Two-column PDF must route to VISION to preserve reading order."""

    def test_path_is_vision(self):
        d = _router().classify(CORPUS / "sample_multicolumn.pdf")
        assert d.path == ExtractionPath.VISION

    def test_reason_indicates_multicolumn(self):
        gt = _gt("sample_multicolumn.pdf")
        d = _router().classify(CORPUS / "sample_multicolumn.pdf")
        assert gt["expected_routing_reason_contains"] in d.reason


class TestRoutingScreenshotJpg:
    """JPG image must route to VISION by extension alone."""

    def test_path_is_vision(self):
        d = _router().classify(CORPUS / "sample_screenshot.jpg")
        assert d.path == ExtractionPath.VISION

    def test_file_type_is_jpg(self):
        d = _router().classify(CORPUS / "sample_screenshot.jpg")
        assert d.file_type == "jpg"

    def test_reason_is_image_format(self):
        d = _router().classify(CORPUS / "sample_screenshot.jpg")
        assert "image_format" in d.reason


class TestRoutingLegacyDoc:
    """Legacy .doc without LibreOffice must dead-letter."""

    def test_path_is_dead_letter(self):
        d = _router().classify(CORPUS / "sample_legacy.doc")
        assert d.path == ExtractionPath.DEAD_LETTER

    def test_dead_letter_reason_mentions_libreoffice(self):
        gt = _gt("sample_legacy.doc")
        d = _router().classify(CORPUS / "sample_legacy.doc")
        assert gt["expected_dead_letter_reason_contains"] in d.dead_letter_reason


class TestRoutingCorruptPdf:
    """Zero-byte PDF must dead-letter immediately."""

    def test_path_is_dead_letter(self):
        d = _router().classify(CORPUS / "sample_corrupt.pdf")
        assert d.path == ExtractionPath.DEAD_LETTER

    def test_dead_letter_reason_mentions_zero_byte(self):
        gt = _gt("sample_corrupt.pdf")
        d = _router().classify(CORPUS / "sample_corrupt.pdf")
        assert gt["expected_dead_letter_reason_contains"] in d.dead_letter_reason


# ---------------------------------------------------------------------------
# Phase 0 fixture routing sanity checks (regression)
# ---------------------------------------------------------------------------

class TestRoutingPhase0Fixtures:
    """Phase 0 corpus formats must still route correctly after Phase 2 changes."""

    def test_text_pdf_routes_text(self):
        d = _router().classify(CORPUS / "sample_ca_india_synthetic.pdf")
        assert d.path == ExtractionPath.TEXT

    def test_docx_routes_text(self):
        d = _router().classify(CORPUS / "sample_ca_india_synthetic.docx")
        assert d.path == ExtractionPath.TEXT

    def test_jpg_routes_vision(self):
        d = _router().classify(CORPUS / "sample_ca_india_synthetic.jpg")
        assert d.path == ExtractionPath.VISION

    def test_txt_routes_text(self):
        d = _router().classify(CORPUS / "sample_01_Accountant.txt")
        assert d.path == ExtractionPath.TEXT

    def test_nonexistent_file_dead_letters(self):
        d = _router().classify(CORPUS / "does_not_exist.pdf")
        assert d.path == ExtractionPath.DEAD_LETTER
        assert "not_found" in d.dead_letter_reason


# ---------------------------------------------------------------------------
# Escalation loop tests (mock extractor)
# ---------------------------------------------------------------------------

class _AlwaysFailExtractor:
    """Extractor stub that always raises ExtractionError."""

    from extraction.base import ExtractionError

    def extract(self, schema, images=None, text=None) -> dict:
        from extraction.base import ExtractionError
        raise ExtractionError("forced failure", source_file="test", attempts=1)

    @property
    def model_name(self) -> str:
        return "mock-fail"


class _CountingExtractor:
    """Extractor stub that records calls and returns a valid-looking result on nth call."""

    def __init__(self, succeed_on: int = 1):
        self.calls: list[dict] = []
        self._succeed_on = succeed_on

    def extract(self, schema, images=None, text=None) -> dict:
        self.calls.append({"images": images, "text": text})
        if len(self.calls) >= self._succeed_on:
            return {"full_name": "Test Candidate", "emails": ["test@example.com"]}
        # Return missing full_name → score will fail → escalate
        return {"full_name": None, "emails": []}

    @property
    def model_name(self) -> str:
        return "mock-counting"


class TestEscalationLoop:
    """extract_with_escalation escalates tier on score failure."""

    def test_dead_letter_returns_none(self):
        """Dead-lettered files must return (None, decision) without calling extractor."""
        extractor = _CountingExtractor()
        raw, decision = extract_with_escalation(
            CORPUS / "sample_corrupt.pdf",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        assert raw is None
        assert decision.path == ExtractionPath.DEAD_LETTER
        assert len(extractor.calls) == 0  # extractor never called

    def test_dead_letter_legacy_doc(self):
        extractor = _CountingExtractor()
        raw, decision = extract_with_escalation(
            CORPUS / "sample_legacy.doc",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        assert raw is None
        assert decision.path == ExtractionPath.DEAD_LETTER
        assert len(extractor.calls) == 0

    def test_successful_extraction_returns_result(self):
        """Extractor that succeeds immediately → returns result with no escalation."""
        extractor = _CountingExtractor(succeed_on=1)
        raw, decision = extract_with_escalation(
            CORPUS / "sample_ca_india_synthetic.txt",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        assert raw is not None
        assert raw.get("full_name") == "Test Candidate"
        assert len(extractor.calls) == 1

    def test_score_failure_triggers_escalation(self):
        """Extractor that returns null full_name on first call must trigger escalation.

        The txt file starts on TEXT; failing score bumps to HYBRID then VISION.
        Extractor succeeds on 2nd call (HYBRID) — so calls == 2.
        """
        extractor = _CountingExtractor(succeed_on=2)
        raw, decision = extract_with_escalation(
            CORPUS / "sample_ca_india_synthetic.txt",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        assert raw is not None
        assert raw.get("full_name") == "Test Candidate"
        # Should have escalated at least once
        assert len(extractor.calls) >= 2
        assert len(decision.escalation_history) >= 1

    def test_exhausted_tiers_returns_best_effort_with_review_reasons(self):
        """When all tiers fail, returns best-effort dict with _review_reasons."""
        extractor = _CountingExtractor(succeed_on=999)  # never succeeds
        raw, decision = extract_with_escalation(
            CORPUS / "sample_ca_india_synthetic.txt",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        # Should still return the last partial result (not None)
        assert raw is not None
        assert "_review_reasons" in raw
        assert any("escalation_exhausted" in r for r in raw["_review_reasons"])

    def test_vision_file_starts_at_vision_tier(self):
        """VISION-classified file must start at VISION (no TEXT attempt)."""
        extractor = _CountingExtractor(succeed_on=1)
        raw, decision = extract_with_escalation(
            CORPUS / "sample_screenshot.jpg",
            _router(),
            extractor,
            ResumeExtractPayload,
        )
        # Only one call, must have been a VISION call (images is non-None)
        assert len(extractor.calls) == 1
        assert extractor.calls[0]["images"] is not None
        assert extractor.calls[0]["images"] != []


# ---------------------------------------------------------------------------
# Routing module imports and contract tests
# ---------------------------------------------------------------------------

class TestRoutingContracts:
    def test_file_router_is_importable(self):
        from routing import FileRouter
        assert FileRouter is not None

    def test_extraction_path_values(self):
        assert ExtractionPath.TEXT.value == "text"
        assert ExtractionPath.VISION.value == "vision"
        assert ExtractionPath.HYBRID.value == "hybrid"
        assert ExtractionPath.DEAD_LETTER.value == "dead_letter"

    def test_routing_decision_defaults(self):
        d = RoutingDecision(
            path=ExtractionPath.TEXT,
            file_type="pdf",
            reason="test",
        )
        assert d.page_paths == {}
        assert d.escalation_history == []
        assert d.dead_letter_reason is None

    def test_pdf_utils_importable(self):
        from routing.pdf_utils import analyse_pdf_pages, render_pdf_page_to_bytes
        assert callable(analyse_pdf_pages)
        assert callable(render_pdf_page_to_bytes)

    def test_doc_converter_importable(self):
        from routing.doc_converter import find_soffice, convert_doc_to_pdf
        assert callable(find_soffice)
        assert callable(convert_doc_to_pdf)
