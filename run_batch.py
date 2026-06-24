"""
run_batch.py — batch pipeline over a folder of resumes.

Usage:
    python run_batch.py [folder] [--output output.xlsx] [--force]

Defaults:
    folder   test_corpus/files/
    output   output_data/results.xlsx

Pipeline per file:
    route → extract (GeminiExtractor) → normalize → validate → ExcelSink.write

Files already in the workbook (by source_file name) are skipped on re-run
to avoid burning Gemini quota.  Use --force to re-process everything.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from extraction.gemini_extractor import GeminiExtractor
from normalize import normalize_record
from output.excel_sink import ExcelSink
from routing import ExtractionPath, FileRouter, extract_with_escalation
from routing.base import RoutingDecision
from schema import ResumeExtractPayload, ResumeRecord
from validate import validate_record

logger = logging.getLogger(__name__)

_SUPPORTED_EXTS = {
    ".pdf", ".docx", ".doc", ".txt",
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".heic", ".gif",
}


def _source_text(path: Path, path_taken: ExtractionPath) -> str | None:
    """Extract raw text from the file for the hallucination guard (text-path only)."""
    if path_taken != ExtractionPath.TEXT:
        return None
    try:
        ext = path.suffix.lower()
        if ext == ".pdf":
            from pypdf import PdfReader
            return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
        if ext == ".docx":
            from docx import Document
            return "\n".join(p.text for p in Document(str(path)).paragraphs if p.text.strip())
        if ext == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return None


def _dead_letter_record(path: Path, decision: RoutingDecision) -> dict:
    dl_key = hashlib.sha1(str(path.resolve()).encode()).hexdigest()
    return {
        "full_name": None,
        "emails": [],
        "phones": [],
        "meta": {
            "source_file": path.name,
            "source_path": str(path),
            "file_type": decision.file_type,
            "parse_timestamp": datetime.now(timezone.utc).isoformat(),
            "model_used": None,
            "path_taken": "dead_letter",
            "overall_confidence": 0.0,
            "field_confidences": {},
            "needs_review": True,
            "review_reasons": [f"dead_letter:{decision.dead_letter_reason or 'unknown'}"],
            "dedup_key": dl_key,
        },
    }


def run_batch(
    folder: Path,
    output_path: Path,
    *,
    force: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Run the full pipeline over all resume files in `folder`.

    Returns a summary dict with counts, token usage, cost, and wall time.
    """
    folder = folder.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sink = ExcelSink(output_path)
    router = FileRouter()
    extractor = GeminiExtractor(api_key=settings.gemini_api_key)

    files = sorted(
        p for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in _SUPPORTED_EXTS
        and not p.name.startswith(".")
    )

    already_processed = set() if force else sink.get_processed_source_files()

    total = len(files)
    parsed_ok = 0
    routed_review = 0
    dead_lettered = 0
    skipped = 0
    prompt_tokens = 0
    output_tokens = 0

    t0 = time.perf_counter()

    for path in files:
        if path.name in already_processed:
            if verbose:
                print(f"  [SKIP] {path.name}")
            skipped += 1
            continue

        if verbose:
            print(f"  [PROC] {path.name} ...", end=" ", flush=True)

        # Route + extract
        try:
            raw, decision = extract_with_escalation(
                path, router, extractor, ResumeExtractPayload
            )
        except Exception as exc:
            logger.warning("Unhandled error for %s: %s", path.name, exc)
            if verbose:
                print(f"ERROR ({exc})")
            dl = RoutingDecision(
                path=ExtractionPath.DEAD_LETTER,
                file_type=path.suffix.lstrip("."),
                reason="exception",
                dead_letter_reason=str(exc)[:200],
            )
            sink.write(_dead_letter_record(path, dl))
            dead_lettered += 1
            continue

        if raw is None:
            if verbose:
                print(f"dead-letter ({decision.dead_letter_reason})")
            sink.write(_dead_letter_record(path, decision))
            dead_lettered += 1
            continue

        # Token accounting
        usage = raw.pop("_usage", {})
        prompt_tokens += usage.get("prompt_tokens") or 0
        output_tokens += usage.get("output_tokens") or 0

        # Build + normalize + validate
        try:
            record = ResumeRecord.model_validate({
                **raw,
                "meta": {
                    "source_file": path.name,
                    "source_path": str(path),
                    "file_type": path.suffix.lstrip("."),
                    "parse_timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_used": extractor.model_name,
                    "path_taken": decision.path.value,
                    "needs_review": False,
                },
            })
        except Exception as exc:
            logger.warning("Schema error for %s: %s", path.name, exc)
            if verbose:
                print(f"schema-error → dead-letter")
            dl = RoutingDecision(
                path=ExtractionPath.DEAD_LETTER,
                file_type=path.suffix.lstrip("."),
                reason="schema_validation_error",
                dead_letter_reason=str(exc)[:200],
            )
            sink.write(_dead_letter_record(path, dl))
            dead_lettered += 1
            continue

        record_dict = record.model_dump()
        record_dict = normalize_record(record_dict)
        record_dict = validate_record(record_dict, source_text=_source_text(path, decision.path))
        sink.write(record_dict)

        conf = record_dict.get("meta", {}).get("overall_confidence", 0)
        if record_dict.get("meta", {}).get("needs_review"):
            routed_review += 1
            if verbose:
                print(f"review (conf={conf:.2f})")
        else:
            parsed_ok += 1
            if verbose:
                print(f"ok (conf={conf:.2f})")

    elapsed = time.perf_counter() - t0
    est_usd = (prompt_tokens * 0.075 + output_tokens * 0.30) / 1_000_000

    return {
        "total_files": total,
        "parsed_ok": parsed_ok,
        "routed_review": routed_review,
        "dead_lettered": dead_lettered,
        "skipped": skipped,
        "total_prompt_tokens": prompt_tokens,
        "total_output_tokens": output_tokens,
        "estimated_usd": round(est_usd, 6),
        "wall_time_seconds": round(elapsed, 2),
        "output_path": str(output_path),
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Batch resume parser")
    parser.add_argument("folder", nargs="?", default=str(settings.corpus_dir))
    parser.add_argument("--output", default=str(settings.output_dir / "results.xlsx"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print(f"\nBatch run:  {args.folder}")
    print(f"Output:     {args.output}\n")

    summary = run_batch(
        folder=Path(args.folder),
        output_path=Path(args.output),
        force=args.force,
        verbose=True,
    )

    print("\n=== Run Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
