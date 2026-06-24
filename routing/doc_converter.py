"""
Legacy .doc → .pdf/.docx conversion via LibreOffice subprocess.

LibreOffice is not a Python package — this module shells out to soffice.
If LibreOffice is not installed, conversion is impossible and the file is
dead-lettered with a clear reason rather than crashing.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Common install locations across platforms
_SOFFICE_CANDIDATES = [
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/snap/bin/libreoffice",
]


def find_soffice() -> Optional[str]:
    """Return the path to soffice/libreoffice, or None if not found."""
    for candidate in _SOFFICE_CANDIDATES:
        if os.path.isabs(candidate):
            if Path(candidate).exists():
                return candidate
        else:
            # Check PATH
            result = subprocess.run(
                ["which", candidate], capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    return None


def convert_doc_to_pdf(doc_path: Path, dest_dir: Path | None = None) -> Optional[Path]:
    """
    Convert a legacy .doc file to PDF using LibreOffice headless.

    Returns the Path of the output PDF, or None if conversion fails.
    dest_dir defaults to a temp directory.
    """
    soffice = find_soffice()
    if not soffice:
        logger.warning(
            "LibreOffice not found — cannot convert %s. "
            "Install LibreOffice to enable .doc conversion.",
            doc_path.name,
        )
        return None

    if dest_dir is None:
        dest_dir = Path(tempfile.mkdtemp(prefix="doc_convert_"))
    dest_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        soffice,
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(dest_dir),
        str(doc_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.error("soffice conversion failed: %s", result.stderr)
            return None
        out_path = dest_dir / (doc_path.stem + ".pdf")
        if out_path.exists():
            return out_path
        logger.error("soffice ran but output PDF not found at %s", out_path)
        return None
    except subprocess.TimeoutExpired:
        logger.error("soffice timed out converting %s", doc_path.name)
        return None
    except Exception as exc:
        logger.error("soffice conversion error: %s", exc)
        return None
