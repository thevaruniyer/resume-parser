"""
run_batch.py — batch pipeline over a storage connector (local folder or rclone remote).

Usage:
    python run_batch.py [folder] [--output output.xlsx] [--force] [--connector {local,rclone}]

Connector selection (priority order):
    1. --connector CLI flag
    2. CONNECTOR env var (set in .env)
    3. Default: "local"

For the local connector, `folder` positional arg sets the source directory
(default: settings.corpus_dir).  For rclone, RCLONE_REMOTE + RCLONE_PATH
from .env are used; the folder arg is ignored.

Dead-letter entries are written to:
    output_data/dead_letter.jsonl   (one JSON object per line)
    output_data/results.xlsx        Review sheet

Manifest (new/changed-file tracking):
    output_data/manifests/local_<hash>.json
    output_data/manifests/rclone_<remote>_<hash>.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from connectors.base import FileRecord, StorageConnector
from extraction.gemini_extractor import GeminiExtractor
from normalize import normalize_record
from output.excel_sink import ExcelSink
from routing import ExtractionPath, FileRouter, extract_with_escalation
from routing.base import RoutingDecision
from schema import ResumeExtractPayload, ResumeRecord
from validate import validate_record

logger = logging.getLogger(__name__)

_DEAD_LETTER_LOG = settings.output_dir / "dead_letter.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_connector(connector_type: str, folder: Path | None) -> StorageConnector:
    if connector_type == "rclone":
        from connectors.rclone_connector import RcloneConnector
        return RcloneConnector()
    from connectors.local_connector import LocalFolderConnector
    return LocalFolderConnector(folder or settings.corpus_dir)


def _source_text(path: Path, path_taken) -> str | None:
    """Re-read source text for hallucination guard (text-path only)."""
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


def _dead_letter_record(file_record: FileRecord, reason: str) -> dict:
    dl_key = hashlib.sha1(file_record.path.encode()).hexdigest()
    return {
        "full_name": None,
        "emails": [],
        "phones": [],
        "meta": {
            "source_file": file_record.name,
            "source_path": file_record.path,
            "file_type": file_record.file_type,
            "parse_timestamp": datetime.now(timezone.utc).isoformat(),
            "model_used": None,
            "path_taken": "dead_letter",
            "overall_confidence": 0.0,
            "field_confidences": {},
            "needs_review": True,
            "review_reasons": [f"dead_letter:{reason}"],
            "dedup_key": dl_key,
        },
    }


def _append_dead_letter(file_record: FileRecord, reason: str) -> None:
    _DEAD_LETTER_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "filename": file_record.name,
        "path": file_record.path,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _DEAD_LETTER_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main batch function
# ---------------------------------------------------------------------------

def run_batch(
    folder: Path | None = None,
    output_path: Path | None = None,
    *,
    force: bool = False,
    connector_type: str | None = None,
    verbose: bool = True,
) -> dict:
    """
    Run the full pipeline over all new/changed files from the connector.

    Returns a summary dict with counts, token usage, cost, and wall time.
    """
    output_path = (output_path or settings.output_dir / "results.xlsx").resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    connector_type = connector_type or settings.connector
    connector = _build_connector(connector_type, folder)

    sink = ExcelSink(output_path)
    router = FileRouter()
    extractor = GeminiExtractor(api_key=settings.gemini_api_key)

    # Delta: which files are new or changed?
    manifest = connector.load_manifest()
    if force:
        files: list[FileRecord] = connector.list_files()
        if verbose:
            print(f"  [FORCE] bypassing manifest — treating all {len(files)} file(s) as new")
    else:
        files = connector.delta(manifest)

    files_in_delta = len(files)
    downloaded = 0
    parsed_ok = 0
    routed_review = 0
    dead_lettered = 0
    skipped = 0
    prompt_tokens = 0
    output_tokens = 0

    t0 = time.perf_counter()

    for file_record in files:
        if verbose:
            print(f"  [PROC] {file_record.name} ...", end=" ", flush=True)

        # Download (no-op for local; rclone copies to tmp/)
        try:
            local_path = connector.download(file_record)
            downloaded += 1
        except Exception as exc:
            logger.warning("Download failed for %s: %s", file_record.name, exc)
            if verbose:
                print(f"download-error → dead-letter")
            record = _dead_letter_record(file_record, f"download_failed:{exc!s:.100}")
            sink.write(record)
            _append_dead_letter(file_record, f"download_failed:{exc!s:.100}")
            dead_lettered += 1
            manifest[file_record.name] = {
                "hash": file_record.file_hash,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            connector.save_manifest(manifest)
            continue

        # Route + extract (with one retry on transient failure)
        raw = None
        decision = None
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                raw, decision = extract_with_escalation(
                    local_path, router, extractor, ResumeExtractPayload
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(
                        "Extraction attempt 1 failed for %s: %s — retrying in 5s",
                        file_record.name, exc,
                    )
                    time.sleep(5)

        connector.cleanup_downloaded(local_path)

        if last_exc is not None:
            logger.warning("Extraction failed after retry for %s: %s", file_record.name, last_exc)
            if verbose:
                print(f"ERROR ({last_exc})")
            record = _dead_letter_record(file_record, str(last_exc)[:200])
            sink.write(record)
            _append_dead_letter(file_record, str(last_exc)[:200])
            dead_lettered += 1
            manifest[file_record.name] = {
                "hash": file_record.file_hash,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            connector.save_manifest(manifest)
            continue

        if raw is None:
            if verbose:
                print(f"dead-letter ({decision.dead_letter_reason})")
            reason = decision.dead_letter_reason or "unknown"
            record = _dead_letter_record(file_record, reason)
            sink.write(record)
            _append_dead_letter(file_record, reason)
            dead_lettered += 1
            manifest[file_record.name] = {
                "hash": file_record.file_hash,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            connector.save_manifest(manifest)
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
                    "source_file": file_record.name,
                    "source_path": file_record.path,
                    "file_type": file_record.file_type,
                    "parse_timestamp": datetime.now(timezone.utc).isoformat(),
                    "model_used": extractor.model_name,
                    "path_taken": decision.path.value,
                    "needs_review": False,
                },
            })
        except Exception as exc:
            logger.warning("Schema error for %s: %s", file_record.name, exc)
            if verbose:
                print(f"schema-error → dead-letter")
            record_dict = _dead_letter_record(file_record, f"schema_error:{exc!s:.200}")
            sink.write(record_dict)
            _append_dead_letter(file_record, f"schema_error:{exc!s:.200}")
            dead_lettered += 1
            manifest[file_record.name] = {
                "hash": file_record.file_hash,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            connector.save_manifest(manifest)
            continue

        record_dict = record.model_dump()
        record_dict = normalize_record(record_dict)
        record_dict = validate_record(
            record_dict,
            source_text=_source_text(local_path, decision.path),
        )
        sink.write(record_dict)

        # Update manifest after successful write
        manifest[file_record.name] = {
            "hash": file_record.file_hash,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
        connector.save_manifest(manifest)

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
        "connector": connector_type,
        "files_in_delta": files_in_delta,
        "downloaded": downloaded,
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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Batch resume parser")
    parser.add_argument("folder", nargs="?", default=None,
                        help="Local folder (local connector only; default: corpus_dir)")
    parser.add_argument("--output", default=str(settings.output_dir / "results.xlsx"))
    parser.add_argument("--force", action="store_true",
                        help="Bypass manifest — process all files")
    parser.add_argument("--connector", choices=["local", "rclone"], default=None,
                        help="Override CONNECTOR env var")
    args = parser.parse_args()

    connector_type = args.connector or settings.connector
    folder = Path(args.folder) if args.folder else None

    print(f"\nConnector:  {connector_type}")
    if connector_type == "local":
        print(f"Folder:     {folder or settings.corpus_dir}")
    else:
        print(f"Remote:     {settings.rclone_remote}:{settings.rclone_path}")
    print(f"Output:     {args.output}\n")

    summary = run_batch(
        folder=folder,
        output_path=Path(args.output),
        force=args.force,
        connector_type=connector_type,
        verbose=True,
    )

    print("\n=== Run Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
