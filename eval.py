"""
eval.py — Verification loop script for resume-parser.

Runs the full pipeline over the test corpus against ground-truth labels and prints:
  per-field accuracy, list-completeness, schema-valid %, hallucination %,
  $/resume, review-rate, per-format breakdown.

Usage:
    uv run python eval.py
    uv run python eval.py --corpus test_corpus/files --gt ground_truth
    uv run python eval.py --json      # machine-readable JSON output
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ground-truth comparison (full GT records from ground_truth/*.json)
# ---------------------------------------------------------------------------

def compare_record(extracted: dict, gt: dict) -> dict:
    """
    Compare extracted record against a ground-truth record (ground_truth/ format).
    Returns {"hits": dict, "misses": dict, "field_scores": dict[str, float]}.
    """
    hits: dict[str, str] = {}
    misses: dict[str, str] = {}

    def check(field: str, passed: bool, note: str = "") -> None:
        (hits if passed else misses)[field] = note or ("ok" if passed else "mismatch")

    # full_name (case-insensitive exact match; both null is also ok)
    gt_name = (gt.get("full_name") or "").strip()
    ex_name = (extracted.get("full_name") or "").strip()
    if not gt_name:
        check("full_name", not ex_name, "both_null" if not ex_name else f"expected_null got={ex_name!r}")
    else:
        check("full_name", gt_name.lower() == ex_name.lower(),
              f"expected={gt_name!r} got={ex_name!r}")

    # emails (every GT email must appear in extracted list, substring match)
    gt_emails = [e.lower() for e in (gt.get("emails") or [])]
    ex_emails = [e.lower() for e in (extracted.get("emails") or [])]
    if gt_emails:
        found = all(any(ge in xe for xe in ex_emails) for ge in gt_emails)
        check("emails", found, f"gt={gt_emails} extracted={ex_emails}")
    else:
        hits["emails"] = "no_gt_emails"

    # list counts: extracted >= GT count
    for list_field in ("education", "qualifications", "work_experience", "articleship_internships"):
        gt_count = len(gt.get(list_field) or [])
        ex_count = len(extracted.get(list_field) or [])
        key = f"{list_field}_count"
        if gt_count > 0:
            check(key, ex_count >= gt_count, f"expected>={gt_count} got={ex_count}")
        else:
            hits[key] = "no_gt_entries"

    field_scores: dict[str, float] = {f: 1.0 for f in hits}
    field_scores.update({f: 0.0 for f in misses})
    return {"hits": hits, "misses": misses, "field_scores": field_scores}


# ---------------------------------------------------------------------------
# Golden-file comparison (tests/golden/*.json format, used by regression tests)
# ---------------------------------------------------------------------------

def compare_with_golden_file(record: dict, golden: dict) -> tuple[bool, list[str]]:
    """
    Compare extracted record against a golden expected file (tests/golden/ format).
    Returns (passed: bool, failures: list[str]).
    """
    failures: list[str] = []
    kf = golden.get("key_fields", {})

    # full_name: substring match (case-insensitive)
    if "full_name" in kf:
        expected = kf["full_name"]
        actual = (record.get("full_name") or "").strip()
        if expected.lower() not in actual.lower():
            failures.append(f"full_name: expected name containing '{expected}', got '{actual}'")

    # full_name_is_null_or_empty
    if kf.get("full_name_is_null_or_empty"):
        actual = (record.get("full_name") or "").strip()
        if actual:
            failures.append(f"full_name: expected null/empty (garbled source), got '{actual}'")

    # emails: every must-contain substring must appear in the joined emails string
    if "emails_must_contain" in kf:
        joined = " ".join(e.lower() for e in (record.get("emails") or []))
        for substring in kf["emails_must_contain"]:
            if substring.lower() not in joined:
                failures.append(
                    f"emails: expected to contain '{substring}', got {record.get('emails')}"
                )

    # list counts (minimum thresholds)
    count_map = {
        "education_count_min": "education",
        "qualifications_count_min": "qualifications",
        "work_experience_count_min": "work_experience",
        "articleship_count_min": "articleship_internships",
    }
    for key, field in count_map.items():
        if key in kf:
            expected_min = kf[key]
            actual_count = len(record.get(field) or [])
            if actual_count < expected_min:
                failures.append(
                    f"{field}: got {actual_count}, expected >= {expected_min}"
                )

    return len(failures) == 0, failures


# ---------------------------------------------------------------------------
# Single-file extraction helper (mirrors run_batch without Excel write)
# ---------------------------------------------------------------------------

def _extract_file(
    filepath: Path,
    extractor,
    router,
) -> tuple[dict | None, str, float, int, int]:
    """
    Extract a single file through the full pipeline (no Excel write).
    Returns: (record_dict | None, path_taken_str, confidence, prompt_tokens, output_tokens)
    """
    from routing import extract_with_escalation, ExtractionPath
    from schema import ResumeExtractPayload, ResumeRecord
    from normalize import normalize_record
    from validate import validate_record

    def _source_text(path: Path, path_taken) -> str | None:
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

    try:
        raw, decision = extract_with_escalation(filepath, router, extractor, ResumeExtractPayload)
    except Exception as exc:
        logger.warning("Extraction failed for %s: %s", filepath.name, exc)
        return None, f"error:{exc!s:.80}", 0.0, 0, 0

    if raw is None:
        return None, decision.dead_letter_reason or "dead_letter", 0.0, 0, 0

    usage = raw.pop("_usage", {})
    prompt_tokens = usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0

    try:
        record = ResumeRecord.model_validate({
            **raw,
            "meta": {
                "source_file": filepath.name,
                "source_path": str(filepath),
                "file_type": filepath.suffix.lstrip(".").lower(),
                "parse_timestamp": datetime.now(timezone.utc).isoformat(),
                "model_used": extractor.model_name,
                "path_taken": decision.path.value,
                "needs_review": False,
            },
        })
    except Exception as exc:
        logger.warning("Schema error for %s: %s", filepath.name, exc)
        return None, "schema_error", 0.0, prompt_tokens, output_tokens

    record_dict = record.model_dump()
    record_dict = normalize_record(record_dict)
    record_dict = validate_record(record_dict, source_text=_source_text(filepath, decision.path))

    confidence = record_dict.get("meta", {}).get("overall_confidence", 0.0)
    return record_dict, decision.path.value, confidence, prompt_tokens, output_tokens


# ---------------------------------------------------------------------------
# Main evaluation runner
# ---------------------------------------------------------------------------

def run_evaluation(corpus_dir: Path, gt_dir: Path) -> dict:
    """
    Run the pipeline on all GT-labeled corpus files and compute metrics.
    Returns a structured metrics dict.
    """
    from extraction.fallback_extractor import build_fallback_extractor
    from routing import FileRouter

    extractor = build_fallback_extractor()
    router = FileRouter()

    gt_files = sorted(gt_dir.glob("*.json"))
    if not gt_files:
        raise FileNotFoundError(f"No ground truth JSON files in {gt_dir}")

    _FIELD_KEYS = frozenset(
        ("full_name", "emails", "phones", "education", "qualifications",
         "work_experience", "articleship_internships")
    )

    def _is_field_gt(gt: dict) -> bool:
        """True if this GT file has at least one field-extraction label."""
        return any(k in gt for k in _FIELD_KEYS)

    def _infer_corpus_path(stem: str) -> Path | None:
        """Find the best corpus file for a GT stem name."""
        # Prefer PDF → TXT → JPG → others for multi-format stems
        for ext in (".pdf", ".txt", ".jpg", ".jpeg", ".docx", ".doc"):
            p = corpus_dir / (stem + ext)
            if p.exists():
                return p
        matches = list(corpus_dir.glob(f"{stem}.*"))
        return matches[0] if matches else None

    results = []
    t0 = time.perf_counter()
    total_prompt_tokens = 0
    total_output_tokens = 0

    for gt_path in gt_files:
        gt = json.loads(gt_path.read_text())

        # Skip routing-only GT files (no field labels)
        if not _is_field_gt(gt):
            continue

        # Resolve corpus path: explicit field first, then infer from stem
        corpus_filename = gt.get("corpus_file")
        if corpus_filename:
            corpus_path = corpus_dir / corpus_filename
        else:
            corpus_path = _infer_corpus_path(gt_path.stem)

        if corpus_path is None or not corpus_path.exists():
            logger.warning("Corpus file not found for GT %s", gt_path.name)
            continue

        corpus_filename = corpus_path.name
        print(f"  [EVAL] {corpus_filename} ...", end=" ", flush=True)
        record, path_taken, confidence, pt, ot = _extract_file(corpus_path, extractor, router)
        total_prompt_tokens += pt
        total_output_tokens += ot

        if record is None:
            print(f"FAILED ({path_taken})")
            results.append({
                "file": corpus_filename,
                "path_taken": path_taken,
                "failed": True,
                "schema_valid": False,
                "hallucination": False,
                "needs_review": True,
                "confidence": 0.0,
                "comparison": {
                    "hits": {},
                    "misses": {"extraction": "failed"},
                    "field_scores": {},
                },
            })
            continue

        comparison = compare_record(record, gt)
        hallucination = any(
            "hallucination_suspect" in r
            for r in (record.get("meta", {}).get("review_reasons") or [])
        )
        needs_review = bool(record.get("meta", {}).get("needs_review"))

        pass_str = "PASS" if not comparison["misses"] else f"FAIL({','.join(comparison['misses'])})"
        print(f"{pass_str}  path={path_taken}  conf={confidence:.2f}")

        results.append({
            "file": corpus_filename,
            "path_taken": path_taken,
            "failed": False,
            "schema_valid": True,
            "hallucination": hallucination,
            "needs_review": needs_review,
            "confidence": confidence,
            "comparison": comparison,
        })

    elapsed = time.perf_counter() - t0
    n = len(results)
    if n == 0:
        raise RuntimeError("No files evaluated — check corpus_dir and gt_dir")

    est_usd = (total_prompt_tokens * 0.075 + total_output_tokens * 0.30) / 1_000_000

    # Aggregate per-field accuracy
    all_field_keys: set[str] = set()
    for r in results:
        all_field_keys.update(r["comparison"]["field_scores"])

    per_field_accuracy: dict[str, float] = {}
    for fk in sorted(all_field_keys):
        scores = [
            r["comparison"]["field_scores"][fk]
            for r in results
            if fk in r["comparison"]["field_scores"]
        ]
        per_field_accuracy[fk] = round(sum(scores) / len(scores), 4) if scores else 0.0

    all_scores = [v for r in results for v in r["comparison"]["field_scores"].values()]
    overall_accuracy = round(sum(all_scores) / len(all_scores), 4) if all_scores else 0.0

    schema_valid_count = sum(1 for r in results if r["schema_valid"])
    hallucination_count = sum(1 for r in results if r["hallucination"])
    needs_review_count = sum(1 for r in results if r["needs_review"])
    failed_count = sum(1 for r in results if r["failed"])

    # Per-format breakdown
    per_format: dict[str, dict] = {}
    for r in results:
        fmt = r["path_taken"] if not r["failed"] else "error"
        if fmt not in per_format:
            per_format[fmt] = {"count": 0, "scores": [], "review": 0}
        per_format[fmt]["count"] += 1
        per_format[fmt]["scores"].extend(r["comparison"]["field_scores"].values())
        if r["needs_review"]:
            per_format[fmt]["review"] += 1

    per_format_metrics = {
        fmt: {
            "count": d["count"],
            "accuracy": round(sum(d["scores"]) / len(d["scores"]), 4) if d["scores"] else 0.0,
            "review_count": d["review"],
            "review_rate": round(d["review"] / d["count"], 4) if d["count"] else 0.0,
        }
        for fmt, d in per_format.items()
    }

    return {
        "n_files": n,
        "n_failed": failed_count,
        "n_evaluated": n - failed_count,
        "overall_accuracy": overall_accuracy,
        "per_field_accuracy": per_field_accuracy,
        "schema_valid_pct": round(schema_valid_count / n, 4) if n else 0.0,
        "hallucination_pct": round(hallucination_count / n, 4) if n else 0.0,
        "review_rate": round(needs_review_count / n, 4) if n else 0.0,
        "cost_per_resume": round(est_usd / n, 6) if n else 0.0,
        "total_cost_usd": round(est_usd, 6),
        "wall_time_seconds": round(elapsed, 2),
        "per_format": per_format_metrics,
        "per_file": results,
    }


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(metrics: dict) -> None:
    n = metrics["n_files"]
    n_eval = metrics["n_evaluated"]

    print("\n" + "=" * 58)
    print("  Resume Parser — Evaluation Report")
    print("=" * 58)
    print(f"  Files evaluated : {n}  ({n_eval} extracted, {metrics['n_failed']} failed)")
    print(f"  Wall time       : {metrics['wall_time_seconds']:.1f}s")
    print()

    print("--- Per-Field Accuracy ---")
    for field, acc in metrics["per_field_accuracy"].items():
        bar = "#" * int(acc * 20)
        print(f"  {field:<34} {acc*100:5.1f}%  [{bar:<20}]")
    print(f"\n  Overall accuracy : {metrics['overall_accuracy']*100:.1f}%  (target ≥95%)")
    print()

    print("--- Schema & Quality ---")
    print(f"  Schema-valid     : {metrics['schema_valid_pct']*100:.1f}%   (target ≥99%)")
    print(f"  Hallucination    : {metrics['hallucination_pct']*100:.1f}%   (target <1%)")
    print(f"  Review-rate      : {metrics['review_rate']*100:.1f}%   (target ≤15%)")
    print()

    print("--- Cost ---")
    print(f"  $/resume         : ${metrics['cost_per_resume']:.6f}")
    print(f"  Total cost       : ${metrics['total_cost_usd']:.6f}")
    print()

    print("--- Per-Format Breakdown ---")
    for fmt, d in metrics["per_format"].items():
        print(
            f"  {fmt:<12}  files={d['count']}  "
            f"accuracy={d['accuracy']*100:.1f}%  "
            f"review={d['review_rate']*100:.1f}%"
        )
    print()

    targets_met = (
        metrics["schema_valid_pct"] >= 0.99
        and metrics["hallucination_pct"] < 0.01
        and metrics["review_rate"] <= 0.15
    )
    print(f"  Targets  : ≥95% accuracy | ≥99% schema-valid | <1% hallucination | ≤15% review")
    print(f"  Quality  : {'PASS' if targets_met else 'PARTIAL — see per-field accuracy above'}")
    print("=" * 58)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Resume parser evaluation harness")
    parser.add_argument("--corpus", default="test_corpus/files",
                        help="Directory of corpus files (default: test_corpus/files)")
    parser.add_argument("--gt", default="ground_truth",
                        help="Directory of ground-truth JSON files (default: ground_truth)")
    parser.add_argument("--json", dest="json_output", action="store_true",
                        help="Print JSON report instead of formatted output")
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    gt_dir = Path(args.gt)

    if not corpus_dir.exists():
        print(f"ERROR: corpus dir not found: {corpus_dir}", file=sys.stderr)
        sys.exit(1)
    if not gt_dir.exists():
        print(f"ERROR: ground truth dir not found: {gt_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Corpus       : {corpus_dir.resolve()}")
    print(f"Ground truth : {gt_dir.resolve()}")
    print()

    metrics = run_evaluation(corpus_dir, gt_dir)

    if args.json_output:
        print(json.dumps(metrics, indent=2, default=str))
    else:
        print_report(metrics)
