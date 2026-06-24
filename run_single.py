"""
run_single.py — dev loop: one file → print extracted record as pretty JSON.

Usage:
    python run_single.py <path/to/resume.jpg>
    python run_single.py <path/to/resume.pdf>
    python run_single.py <path/to/resume.txt>

The file type determines the extraction path:
    jpg/png/tiff/heic → vision (image bytes sent to Gemini)
    pdf               → text layer extracted first; falls back to vision (Phase 2)
    txt/docx          → text path
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from extraction.gemini_extractor import GeminiExtractor
from normalize import normalize_record
from schema import MetaBlock, ResumeExtractPayload, ResumeRecord
from validate import validate_record

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".gif"}
_TEXT_EXTS = {".txt"}
_PDF_EXT = ".pdf"
_DOCX_EXT = ".docx"


def extract_pdf_text(path: Path) -> str:
    """Extract text layer from a PDF using pypdf."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def extract_docx_text(path: Path) -> str:
    """Extract text from a DOCX file."""
    from docx import Document

    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def run(file_path: str) -> None:
    path = Path(file_path).resolve()
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    ext = path.suffix.lower()
    extractor = GeminiExtractor(api_key=settings.gemini_api_key)
    source_text: str | None = None

    # --- determine extraction path ---
    if ext in _IMAGE_EXTS:
        path_taken = "vision"
        raw = extractor.extract(schema=ResumeExtractPayload, images=[path])

    elif ext == _PDF_EXT:
        text = extract_pdf_text(path)
        if len(text.strip()) > 50:
            path_taken = "text"
            source_text = text
            raw = extractor.extract(schema=ResumeExtractPayload, text=text)
        else:
            # Empty/garbled text layer → fall back to vision
            path_taken = "vision"
            raw = extractor.extract(schema=ResumeExtractPayload, images=[path])

    elif ext == _DOCX_EXT:
        text = extract_docx_text(path)
        path_taken = "text"
        source_text = text
        raw = extractor.extract(schema=ResumeExtractPayload, text=text)

    elif ext in _TEXT_EXTS:
        text = path.read_text(encoding="utf-8")
        path_taken = "text"
        source_text = text
        raw = extractor.extract(schema=ResumeExtractPayload, text=text)

    else:
        print(f"Unsupported file type: {ext}", file=sys.stderr)
        sys.exit(1)

    usage = raw.pop("_usage", {})

    # Build full ResumeRecord with provenance
    record = ResumeRecord.model_validate({
        **raw,
        "meta": {
            "source_file": path.name,
            "source_path": str(path),
            "file_type": ext.lstrip("."),
            "parse_timestamp": datetime.now(timezone.utc).isoformat(),
            "model_used": extractor.model_name,
            "path_taken": path_taken,
            "needs_review": False,
        },
    })

    output = record.model_dump()
    output = normalize_record(output)
    output = validate_record(output, source_text=source_text)
    output["_cost"] = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        # Gemini Flash pricing (approximate, free tier = $0): ~$0.075/1M input, $0.30/1M output
        "estimated_usd": (
            ((usage.get("prompt_tokens") or 0) * 0.075
             + (usage.get("output_tokens") or 0) * 0.30) / 1_000_000
        ),
    }

    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_single.py <path/to/resume>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
