"""
PDF utilities — text extraction, page rendering, and layout analysis.

Uses pymupdf (fitz) for rendering and layout analysis;
pypdf as a lightweight fallback for pure text extraction.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PageAnalysis:
    page_index: int          # 0-based
    text: str                # extracted text (may be empty)
    has_text_layer: bool     # True if text layer has meaningful content
    is_image_only: bool      # True if page is purely rasterized
    is_multicolumn: bool     # True if two distinct x-origin clusters detected
    image_count: int         # number of embedded images on the page
    page_width: float
    page_height: float


def analyse_pdf_pages(path: Path) -> list[PageAnalysis]:
    """
    Open a PDF with pymupdf and return per-page analysis.
    Raises FileNotFoundError or fitz.FileDataError on corrupt/unreadable files.
    """
    import pymupdf  # fitz

    doc = pymupdf.open(str(path))
    results = []

    for idx, page in enumerate(doc):
        text = page.get_text("text") or ""
        blocks = page.get_text("blocks") or []  # each: (x0,y0,x1,y1,text,block_no,type)

        # Image count
        image_list = page.get_images(full=False)
        image_count = len(image_list)

        page_width = page.rect.width
        page_height = page.rect.height

        # has_text_layer: page has any text at all
        has_text_layer = bool(text.strip())

        # is_image_only: images present AND no text layer
        is_image_only = image_count > 0 and not has_text_layer

        # Two-column detection via x-origin clustering
        is_multicolumn = _detect_multicolumn(blocks, page_width)

        results.append(PageAnalysis(
            page_index=idx,
            text=text,
            has_text_layer=has_text_layer,
            is_image_only=is_image_only,
            is_multicolumn=is_multicolumn,
            image_count=image_count,
            page_width=page_width,
            page_height=page_height,
        ))

    doc.close()
    return results


def render_pdf_page_to_bytes(path: Path, page_index: int = 0, dpi: int = 150) -> bytes:
    """Render a single PDF page to PNG bytes (for vision input)."""
    import pymupdf

    doc = pymupdf.open(str(path))
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def pdf_to_image_paths(path: Path, dpi: int = 150, dest_dir: Path | None = None) -> list[Path]:
    """
    Render all PDF pages to PNG files and return their paths.
    dest_dir defaults to a temp sibling directory.
    """
    import pymupdf
    import tempfile

    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="pdf_pages_"))
    dest_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(str(path))
    out_paths = []
    for idx, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi)
        out = dest_dir / f"page_{idx:04d}.png"
        pix.save(str(out))
        out_paths.append(out)
    doc.close()
    return out_paths


def is_pdf_encrypted(path: Path) -> bool:
    """Return True if the PDF is password-protected."""
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
        encrypted = doc.is_encrypted
        doc.close()
        return encrypted
    except Exception:
        return True  # treat unreadable as effectively encrypted/corrupt


def _detect_multicolumn(blocks: list, page_width: float, threshold: float = 0.30) -> bool:
    """
    Detect two-column layout by checking whether text blocks originate from
    two distinct horizontal bands (left and right columns).

    Heuristic: if blocks have x0 values both below 40% and above 50% of page
    width (with at least 2 blocks in each zone), classify as multicolumn.
    """
    if page_width <= 0:
        return False

    text_blocks = [b for b in blocks if len(b) >= 5 and isinstance(b[4], str) and b[4].strip()]
    if len(text_blocks) < 4:
        return False

    left_zone = page_width * 0.40
    right_zone = page_width * 0.50

    left_count = sum(1 for b in text_blocks if b[0] < left_zone)
    right_count = sum(1 for b in text_blocks if b[0] > right_zone)

    return left_count >= 2 and right_count >= 2
