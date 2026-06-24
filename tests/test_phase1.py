"""
Phase 1 tests — Vision PoC.

DoD:
  [x] sample_ca_india_synthetic.jpg parsed via vision → schema-valid JSON,
      full_name matches ground truth, ≥1 education entry, ≥1 qualification entry
  [x] sample_ca_india_synthetic.pdf parsed via text path → schema-valid JSON,
      ICAI membership_number present and non-null
  [x] Prompt regression canary — full_name non-null on both files
  [x] QwenExtractor stub importable and raises NotImplementedError

These tests call the real Gemini API (free-tier, synthetic-only data).
Classes marked TestJpg*/TestPdf*/TestPromptRegression are skipped when
GEMINI_API_KEY is absent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env once at import time so skipif conditions see the key
load_dotenv(ROOT / ".env")

CORPUS = ROOT / "test_corpus" / "files"
GT_DIR = ROOT / "ground_truth"

with open(GT_DIR / "sample_ca_india_synthetic.json") as _f:
    _CA_GT = json.load(_f)

_GEMINI_KEY_PRESENT = bool(os.getenv("GEMINI_API_KEY"))

_SKIP_API = pytest.mark.skipif(
    not _GEMINI_KEY_PRESENT, reason="GEMINI_API_KEY not set"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_extractor():
    from config import settings
    from extraction.gemini_extractor import GeminiExtractor
    return GeminiExtractor(api_key=settings.gemini_api_key)


def _extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _fuzzy_name_match(extracted: str | None, expected: str) -> bool:
    if not extracted:
        return False
    tokens = expected.lower().split()
    return all(t in extracted.lower() for t in tokens)


# ---------------------------------------------------------------------------
# Module-scoped API fixtures (call Gemini once per test session)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jpg_result():
    if not _GEMINI_KEY_PRESENT:
        pytest.skip("GEMINI_API_KEY not set")
    from schema import ResumeExtractPayload
    extractor = _make_extractor()
    jpg_path = CORPUS / "sample_ca_india_synthetic.jpg"
    raw = extractor.extract(schema=ResumeExtractPayload, images=[jpg_path])
    usage = raw.pop("_usage", {})
    raw["__usage__"] = usage  # keep for the cost report
    return raw


@pytest.fixture(scope="module")
def pdf_result():
    if not _GEMINI_KEY_PRESENT:
        pytest.skip("GEMINI_API_KEY not set")
    from schema import ResumeExtractPayload
    extractor = _make_extractor()
    pdf_path = CORPUS / "sample_ca_india_synthetic.pdf"
    text = _extract_pdf_text(pdf_path)
    raw = extractor.extract(schema=ResumeExtractPayload, text=text)
    usage = raw.pop("_usage", {})
    raw["__usage__"] = usage
    return raw


# ---------------------------------------------------------------------------
# 1 — Vision path (JPG)
# ---------------------------------------------------------------------------

@_SKIP_API
class TestJpgVisionPath:
    def test_returns_dict(self, jpg_result):
        assert isinstance(jpg_result, dict)

    def test_schema_valid(self, jpg_result):
        from schema import ResumeExtractPayload
        data = {k: v for k, v in jpg_result.items() if not k.startswith("__")}
        record = ResumeExtractPayload.model_validate(data)
        assert record is not None

    def test_full_name_non_null(self, jpg_result):
        assert jpg_result.get("full_name"), "full_name must be non-null on vision path"

    def test_full_name_matches_ground_truth(self, jpg_result):
        gt_name = _CA_GT["full_name"]  # "Rahul Mehta"
        assert _fuzzy_name_match(jpg_result.get("full_name"), gt_name), (
            f"Expected name close to '{gt_name}', got '{jpg_result.get('full_name')}'"
        )

    def test_has_at_least_one_education_entry(self, jpg_result):
        edu = jpg_result.get("education", [])
        assert len(edu) >= 1, f"Expected ≥1 education entry, got {len(edu)}"

    def test_has_at_least_one_qualification_entry(self, jpg_result):
        quals = jpg_result.get("qualifications", [])
        assert len(quals) >= 1, f"Expected ≥1 qualification entry, got {len(quals)}"

    def test_ca_qualification_present(self, jpg_result):
        """At least one qualification must relate to the CA credential.
        Accept 'CA', 'Chartered Accountant', 'ICAI' body, or CA-level strings."""
        quals = jpg_result.get("qualifications", [])
        _ca_tokens = {"ca", "chartered accountant", "icai", "foundation", "intermediate", "final"}

        def _is_ca_related(q: dict) -> bool:
            text = " ".join([
                str(q.get("name") or ""),
                str(q.get("body") or ""),
                str(q.get("level") or ""),
            ]).lower()
            return any(t in text for t in _ca_tokens)

        ca = [q for q in quals if _is_ca_related(q)]
        assert ca, f"Expected at least one CA-related qualification entry. Got: {quals}"

    def test_emails_is_list(self, jpg_result):
        assert isinstance(jpg_result.get("emails", []), list)


# ---------------------------------------------------------------------------
# 2 — Text path (PDF)
# ---------------------------------------------------------------------------

@_SKIP_API
class TestPdfTextPath:
    def test_returns_dict(self, pdf_result):
        assert isinstance(pdf_result, dict)

    def test_schema_valid(self, pdf_result):
        from schema import ResumeExtractPayload
        data = {k: v for k, v in pdf_result.items() if not k.startswith("__")}
        record = ResumeExtractPayload.model_validate(data)
        assert record is not None

    def test_full_name_non_null(self, pdf_result):
        assert pdf_result.get("full_name"), "full_name must be non-null on text path"

    def test_icai_membership_number_present(self, pdf_result):
        quals = pdf_result.get("qualifications", [])
        numbers = [q.get("membership_number") for q in quals if q.get("membership_number")]
        assert numbers, f"No ICAI membership_number found. Qualifications: {quals}"

    def test_icai_membership_number_is_123456(self, pdf_result):
        quals = pdf_result.get("qualifications", [])
        numbers = [q.get("membership_number") for q in quals if q.get("membership_number")]
        assert any("123456" in str(n) for n in numbers), (
            f"Expected '123456' in membership numbers, got: {numbers}"
        )

    def test_has_at_least_one_education_entry(self, pdf_result):
        edu = pdf_result.get("education", [])
        assert len(edu) >= 1, f"Expected ≥1 education entry, got {len(edu)}"

    def test_has_at_least_one_qualification(self, pdf_result):
        quals = pdf_result.get("qualifications", [])
        assert len(quals) >= 1, f"Expected ≥1 qualification, got {len(quals)}"


# ---------------------------------------------------------------------------
# 3 — Prompt regression canary
# ---------------------------------------------------------------------------

@_SKIP_API
class TestPromptRegressionCanary:
    """Catches silent breakage from future prompt edits."""

    def test_jpg_full_name_non_null(self, jpg_result):
        assert jpg_result.get("full_name") is not None, (
            "REGRESSION: full_name is null after prompt change (jpg path)"
        )

    def test_pdf_full_name_non_null(self, pdf_result):
        assert pdf_result.get("full_name") is not None, (
            "REGRESSION: full_name is null after prompt change (pdf/text path)"
        )

    def test_jpg_list_fields_not_all_empty(self, jpg_result):
        lists = ["education", "qualifications", "work_experience", "skills"]
        non_empty = [k for k in lists if jpg_result.get(k)]
        assert non_empty, "REGRESSION: ALL list fields empty — prompt likely broken"

    def test_pdf_list_fields_not_all_empty(self, pdf_result):
        lists = ["education", "qualifications", "work_experience", "skills"]
        non_empty = [k for k in lists if pdf_result.get(k)]
        assert non_empty, "REGRESSION: ALL list fields empty — prompt likely broken"


# ---------------------------------------------------------------------------
# 4 — QwenExtractor stub (no API call, always runs)
# ---------------------------------------------------------------------------

class TestQwenExtractorStub:
    def test_qwen_importable(self):
        from extraction.qwen_extractor import QwenExtractor  # noqa: F401

    def test_qwen_is_extractor_subclass(self):
        from extraction.base import Extractor
        from extraction.qwen_extractor import QwenExtractor
        assert issubclass(QwenExtractor, Extractor)

    def test_qwen_extract_raises_not_implemented(self):
        from extraction.qwen_extractor import QwenExtractor
        from schema import ResumeExtractPayload
        stub = QwenExtractor(api_key="dummy")
        with pytest.raises(NotImplementedError):
            stub.extract(schema=ResumeExtractPayload, text="dummy")

    def test_qwen_model_name_contains_qwen(self):
        from extraction.qwen_extractor import QwenExtractor
        assert "qwen" in QwenExtractor(api_key="dummy").model_name.lower()


# ---------------------------------------------------------------------------
# 5 — Cost + accuracy report (non-gating, prints metrics)
# ---------------------------------------------------------------------------

@_SKIP_API
def test_cost_and_accuracy_report(jpg_result, pdf_result, capsys):
    """Prints per-field accuracy and token costs. Does not fail the suite."""
    gt = _CA_GT
    gt_name = gt["full_name"]
    gt_edu_count = len(gt.get("education", []))
    gt_qual_count = len(gt.get("qualifications", []))
    gt_membership = "123456"

    def tick(ok: bool) -> str:
        return "✓" if ok else "✗"

    with capsys.disabled():
        print("\n" + "=" * 60)
        print("Phase 1 — accuracy & cost report")
        print("=" * 60)

        for label, result in [("JPG (vision)", jpg_result), ("PDF (text)", pdf_result)]:
            usage = result.get("__usage__", {})
            print(f"\n--- {label} ---")
            print(f"  Tokens: {usage.get('prompt_tokens')} prompt / "
                  f"{usage.get('output_tokens')} output / "
                  f"{usage.get('total_tokens')} total")
            prompt_t = usage.get("prompt_tokens") or 0
            output_t = usage.get("output_tokens") or 0
            cost = (prompt_t * 0.075 + output_t * 0.30) / 1_000_000
            print(f"  Estimated cost: ${cost:.6f} USD (gemini-2.5-flash paid rates)")

            name_ok = _fuzzy_name_match(result.get("full_name"), gt_name)
            edu_count = len(result.get("education", []))
            qual_count = len(result.get("qualifications", []))
            memberships = [
                q.get("membership_number")
                for q in result.get("qualifications", [])
                if q.get("membership_number")
            ]
            icai_ok = any(gt_membership in str(m) for m in memberships)

            print(f"  {tick(name_ok)} full_name: gt={gt_name!r} | got={result.get('full_name')!r}")
            print(f"  {tick(edu_count >= gt_edu_count)} education entries: gt={gt_edu_count} | got={edu_count}")
            print(f"  {tick(qual_count >= gt_qual_count)} qualification entries: gt={gt_qual_count} | got={qual_count}")
            print(f"  {tick(icai_ok)} ICAI membership: gt={gt_membership!r} | found={memberships}")

        print("=" * 60)
