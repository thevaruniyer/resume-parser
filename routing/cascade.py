"""
FileRouter — implements the routing cascade from CLAUDE.md section 2.4.

Cascade (cheap-first, escalate on quality failure):
  image exts     → VISION
  .txt           → TEXT (no probe needed)
  .pdf           → per-page text-quality probe → TEXT | HYBRID | VISION | DEAD_LETTER
  .docx          → text extraction + thinness check → TEXT | VISION
  .doc           → LibreOffice convert → re-route → TEXT | VISION | DEAD_LETTER
  zero-byte /
  corrupt /
  unknown ext    → DEAD_LETTER

This class does pure classification — it never calls the extractor.
Escalation (retry with a higher-cost path on extraction failure) is handled
by routing/escalation.py.
"""
from __future__ import annotations

import logging
from pathlib import Path

from routing.base import ExtractionPath, Router, RoutingDecision
from routing.text_quality import text_quality_ok

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".gif"}
_PDF_EXT = ".pdf"
_DOCX_EXT = ".docx"
_DOC_EXT = ".doc"
_TEXT_EXTS = {".txt"}

# DOCX: flag as image-heavy if text < this many chars AND file > this many bytes
_DOCX_MIN_TEXT_CHARS = 300
_DOCX_MAX_FILESIZE_FOR_TEXT = 50_000  # 50 KB — thin text vs large file → images inside


class FileRouter(Router):
    """
    Concrete router implementing the full cascade.

    For hybrid PDFs, page_paths in the returned RoutingDecision maps each
    0-indexed page number to TEXT or VISION so the extractor can route per page.
    """

    def classify(self, path: Path) -> RoutingDecision:
        path = Path(path)
        ext = path.suffix.lower()
        file_type = ext.lstrip(".")

        # --- guard: existence + non-zero ---
        if not path.exists():
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="file_not_found",
                dead_letter_reason="file_not_found",
            )
        if path.stat().st_size == 0:
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="zero_byte_file",
                dead_letter_reason="zero_byte_file",
            )

        # --- route by type ---
        if ext in _IMAGE_EXTS:
            return RoutingDecision(
                path=ExtractionPath.VISION, file_type=file_type,
                reason="image_format",
            )

        if ext in _TEXT_EXTS:
            return RoutingDecision(
                path=ExtractionPath.TEXT, file_type=file_type,
                reason="plain_text",
            )

        if ext == _PDF_EXT:
            return self._classify_pdf(path, file_type)

        if ext == _DOCX_EXT:
            return self._classify_docx(path, file_type)

        if ext == _DOC_EXT:
            return self._classify_doc(path, file_type)

        return RoutingDecision(
            path=ExtractionPath.DEAD_LETTER, file_type=file_type,
            reason=f"unsupported_extension:{ext}",
            dead_letter_reason=f"unsupported_extension:{ext}",
        )

    # ------------------------------------------------------------------
    # PDF classification
    # ------------------------------------------------------------------

    def _classify_pdf(self, path: Path, file_type: str) -> RoutingDecision:
        from routing.pdf_utils import analyse_pdf_pages, is_pdf_encrypted

        # Encrypted check
        if is_pdf_encrypted(path):
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="pdf_encrypted",
                dead_letter_reason="pdf_encrypted",
            )

        # Try to analyse pages; treat any exception as corrupt
        try:
            pages = analyse_pdf_pages(path)
        except Exception as exc:
            logger.warning("Cannot analyse PDF %s: %s", path.name, exc)
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason=f"pdf_corrupt:{exc}",
                dead_letter_reason=f"pdf_corrupt:{exc}",
            )

        if not pages:
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="pdf_no_pages",
                dead_letter_reason="pdf_no_pages",
            )

        page_paths: dict[int, ExtractionPath] = {}
        reasons: list[str] = []

        for pa in pages:
            # Image-only page → always vision
            if pa.is_image_only:
                page_paths[pa.page_index] = ExtractionPath.VISION
                reasons.append(f"p{pa.page_index}:image_only")
                continue

            # No text layer → vision
            if not pa.has_text_layer:
                page_paths[pa.page_index] = ExtractionPath.VISION
                reasons.append(f"p{pa.page_index}:no_text_layer")
                continue

            # Multicolumn layout → vision (reading order scrambled by linear extraction)
            if pa.is_multicolumn:
                page_paths[pa.page_index] = ExtractionPath.VISION
                reasons.append(f"p{pa.page_index}:multicolumn")
                continue

            # Text quality probe
            qr = text_quality_ok(pa.text)
            if qr.ok:
                page_paths[pa.page_index] = ExtractionPath.TEXT
                reasons.append(f"p{pa.page_index}:text_ok")
            else:
                page_paths[pa.page_index] = ExtractionPath.VISION
                reasons.append(f"p{pa.page_index}:garbled:{qr.reason}")

        # Aggregate page decisions into a single file-level path
        paths_set = set(page_paths.values())

        if paths_set == {ExtractionPath.TEXT}:
            return RoutingDecision(
                path=ExtractionPath.TEXT, file_type=file_type,
                reason="all_pages_text:" + ";".join(reasons),
                page_paths=page_paths,
            )
        elif paths_set == {ExtractionPath.VISION}:
            return RoutingDecision(
                path=ExtractionPath.VISION, file_type=file_type,
                reason="all_pages_vision:" + ";".join(reasons),
                page_paths=page_paths,
            )
        else:
            return RoutingDecision(
                path=ExtractionPath.HYBRID, file_type=file_type,
                reason="mixed_pages:" + ";".join(reasons),
                page_paths=page_paths,
            )

    # ------------------------------------------------------------------
    # DOCX classification
    # ------------------------------------------------------------------

    def _classify_docx(self, path: Path, file_type: str) -> RoutingDecision:
        try:
            from docx import Document
            doc = Document(str(path))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as exc:
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason=f"docx_corrupt:{exc}",
                dead_letter_reason=f"docx_corrupt:{exc}",
            )

        file_size = path.stat().st_size

        # Thinness check: very little text but large file → probably image-heavy
        if len(text.strip()) < _DOCX_MIN_TEXT_CHARS and file_size > _DOCX_MAX_FILESIZE_FOR_TEXT:
            return RoutingDecision(
                path=ExtractionPath.VISION, file_type=file_type,
                reason=f"docx_thin_text:len={len(text)},size={file_size}",
            )

        qr = text_quality_ok(text)
        if qr.ok:
            return RoutingDecision(
                path=ExtractionPath.TEXT, file_type=file_type,
                reason="docx_text_ok",
            )
        else:
            return RoutingDecision(
                path=ExtractionPath.VISION, file_type=file_type,
                reason=f"docx_garbled:{qr.reason}",
            )

    # ------------------------------------------------------------------
    # DOC (legacy) classification
    # ------------------------------------------------------------------

    def _classify_doc(self, path: Path, file_type: str) -> RoutingDecision:
        from routing.doc_converter import convert_doc_to_pdf, find_soffice

        if not find_soffice():
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="doc_libreoffice_not_available",
                dead_letter_reason="doc_libreoffice_not_available",
            )

        # Convert to PDF in a temp dir, then re-classify
        converted = convert_doc_to_pdf(path)
        if converted is None:
            return RoutingDecision(
                path=ExtractionPath.DEAD_LETTER, file_type=file_type,
                reason="doc_conversion_failed",
                dead_letter_reason="doc_conversion_failed",
            )

        decision = self._classify_pdf(converted, "pdf")
        decision.reason = f"doc_converted_then:{decision.reason}"
        decision.file_type = "doc"
        return decision
