"""
Phase 6 — Testing harness: golden-set regression + eval.py verification.

TestEvalHarness  — pure unit tests, no LLM calls.
TestGoldenRegression — full pipeline regression; marked @pytest.mark.regression.
  Skip with: pytest --skip-regression
  CI default: runs (no flag needed).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_DIR = ROOT / "tests" / "golden"
CORPUS_DIR = ROOT / "test_corpus" / "files"
GT_DIR = ROOT / "ground_truth"

_GOLDEN_FILES = [
    "sample_ca_india_synthetic_expected.json",
    "sample_01_Accountant_expected.json",
    "sample_02_Accountant_expected.json",
    "sample_03_Accountant_expected.json",
    "sample_04_Accountant_expected.json",
    "sample_05_Accountant_expected.json",
]


# ---------------------------------------------------------------------------
# TestEvalHarness — no LLM calls
# ---------------------------------------------------------------------------

class TestEvalHarness:
    """Verifies the harness infrastructure without making any LLM API calls."""

    def test_golden_files_exist(self) -> None:
        missing = [f for f in _GOLDEN_FILES if not (GOLDEN_DIR / f).exists()]
        assert not missing, f"Missing golden files: {missing}"

    def test_golden_files_parseable(self) -> None:
        for fname in _GOLDEN_FILES:
            data = json.loads((GOLDEN_DIR / fname).read_text())
            assert "corpus_file" in data, f"{fname}: missing 'corpus_file'"
            assert "key_fields" in data, f"{fname}: missing 'key_fields'"
            corpus_path = CORPUS_DIR / data["corpus_file"]
            assert corpus_path.exists(), f"{fname}: corpus file not found: {corpus_path}"

    def test_eval_module_importable(self) -> None:
        import eval as ev
        assert callable(ev.run_evaluation)
        assert callable(ev.compare_record)
        assert callable(ev.compare_with_golden_file)
        assert callable(ev.print_report)

    def test_compare_record_hit_all_fields(self) -> None:
        import eval as ev

        extracted = {
            "full_name": "Howard Gerrard",
            "emails": ["info@dayjob.com"],
            "education": [{"degree": "BA"}],
            "qualifications": [],
            "work_experience": [{"company": "A"}, {"company": "B"}, {"company": "C"}, {"company": "D"}],
            "articleship_internships": [],
        }
        gt = {
            "full_name": "Howard Gerrard",
            "emails": ["info@dayjob.com"],
            "education": [{"degree": "BA"}],
            "qualifications": [],
            "work_experience": [{"company": "A"}, {"company": "B"}, {"company": "C"}, {"company": "D"}],
            "articleship_internships": [],
        }
        result = ev.compare_record(extracted, gt)
        assert not result["misses"], f"Unexpected misses: {result['misses']}"

    def test_compare_record_catches_wrong_name(self) -> None:
        import eval as ev

        extracted = {"full_name": "Wrong Name", "emails": [], "education": [], "qualifications": [],
                     "work_experience": [], "articleship_internships": []}
        gt = {"full_name": "Correct Name", "emails": [], "education": [], "qualifications": [],
              "work_experience": [], "articleship_internships": []}
        result = ev.compare_record(extracted, gt)
        assert "full_name" in result["misses"]

    def test_compare_record_catches_missing_list_entries(self) -> None:
        import eval as ev

        extracted = {
            "full_name": "Kevin Frank",
            "emails": [],
            "education": [{"degree": "BA"}],  # only 1, GT has 3
            "qualifications": [{"name": "CPA"}],
            "work_experience": [{"company": "A"}, {"company": "B"}],
            "articleship_internships": [],
        }
        gt = {
            "full_name": "Kevin Frank",
            "emails": [],
            "education": [{"degree": "BA"}, {"degree": "MA"}, {"degree": "PhD"}],
            "qualifications": [{"name": "CPA"}],
            "work_experience": [{"company": "A"}, {"company": "B"}],
            "articleship_internships": [],
        }
        result = ev.compare_record(extracted, gt)
        assert "education_count" in result["misses"]
        assert "work_experience_count" not in result["misses"]

    def test_compare_with_golden_file_hit(self) -> None:
        import eval as ev

        golden = json.loads((GOLDEN_DIR / "sample_02_Accountant_expected.json").read_text())
        good_record = {
            "full_name": "Howard Gerrard",
            "emails": ["info@dayjob.com"],
            "education": [{"degree": "BA"}],
            "qualifications": [],
            "work_experience": [{"c": 1}, {"c": 2}, {"c": 3}, {"c": 4}],
            "articleship_internships": [],
        }
        passed, failures = ev.compare_with_golden_file(good_record, golden)
        assert passed, f"Expected pass but got failures: {failures}"

    def test_compare_with_golden_file_null_name(self) -> None:
        """sample_01 expects null name (garbled OCR)."""
        import eval as ev

        golden = json.loads((GOLDEN_DIR / "sample_01_Accountant_expected.json").read_text())
        null_name_record = {
            "full_name": None,
            "emails": [],
            "education": [{"degree": "B.Com"}, {"degree": "M.Com"}],
            "qualifications": [{"name": "CA"}, {"name": "CPA"}],
            "work_experience": [],
            "articleship_internships": [],
        }
        passed, failures = ev.compare_with_golden_file(null_name_record, golden)
        assert passed, f"Expected pass but got failures: {failures}"

    def test_print_report_runs_without_error(self) -> None:
        import eval as ev

        fake_metrics = {
            "n_files": 2, "n_failed": 0, "n_evaluated": 2,
            "overall_accuracy": 0.9,
            "per_field_accuracy": {"full_name": 1.0, "emails": 0.8},
            "schema_valid_pct": 1.0,
            "hallucination_pct": 0.0,
            "review_rate": 0.0,
            "cost_per_resume": 0.0001,
            "total_cost_usd": 0.0002,
            "wall_time_seconds": 5.0,
            "per_format": {"text": {"count": 2, "accuracy": 0.9, "review_count": 0, "review_rate": 0.0}},
            "per_file": [],
        }
        ev.print_report(fake_metrics)  # should not raise


# ---------------------------------------------------------------------------
# TestGoldenRegression — real LLM calls
# ---------------------------------------------------------------------------

@pytest.mark.regression
class TestGoldenRegression:
    """
    Regression harness: runs the full pipeline on each golden corpus file
    and compares against frozen expected values.

    Skipped with:  pytest --skip-regression
    CI default:    runs (no flag needed).
    """

    def _run_and_compare(self, golden_fname: str) -> tuple[bool, list[str], dict | None]:
        """
        Extract the corpus file referenced by the golden file and compare.
        Returns (passed, failures, record).
        Skips (via pytest.skip) when all extractors are quota-exhausted.
        """
        import eval as ev
        from extraction.fallback_extractor import build_fallback_extractor
        from routing import FileRouter

        golden_path = GOLDEN_DIR / golden_fname
        golden = json.loads(golden_path.read_text())
        corpus_path = CORPUS_DIR / golden["corpus_file"]

        extractor = build_fallback_extractor()
        router = FileRouter()
        record, path_taken, _, _, _ = ev._extract_file(corpus_path, extractor, router)

        if record is None:
            _quota_kw = ("quota", "all_models", "rate_limit", "resource_exhausted")
            if any(kw in path_taken.lower() for kw in _quota_kw):
                pytest.skip(
                    f"All extractors quota-exhausted for {golden['corpus_file']} "
                    f"({path_taken[:120]}) — rerun after quota reset"
                )
            return False, [f"extraction_failed: {golden['corpus_file']}"], None

        passed, failures = ev.compare_with_golden_file(record, golden)
        return passed, failures, record

    def test_ca_india_synthetic(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_ca_india_synthetic_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_accountant_01_garbled(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_01_Accountant_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_accountant_02_howard(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_02_Accountant_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_accountant_03_kevin(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_03_Accountant_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_accountant_04_olivia(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_04_Accountant_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_accountant_05_stephen(self) -> None:
        passed, failures, _ = self._run_and_compare("sample_05_Accountant_expected.json")
        assert passed, f"Regression failures: {failures}"

    def test_broken_prompt_detected(self) -> None:
        """
        Verify the harness catches a deliberately broken extraction.
        Mocks the extractor to return wrong values; asserts comparison fails.
        """
        import eval as ev
        from routing import FileRouter
        from schema import ResumeExtractPayload

        golden_path = GOLDEN_DIR / "sample_02_Accountant_expected.json"
        golden = json.loads(golden_path.read_text())

        # Deliberately wrong record (wrong name, no work experience)
        broken_record = {
            "full_name": "Jane Smith Broken",
            "emails": ["wrong@broken.com"],
            "phones": [],
            "education": [{"degree": "BA", "institution": "Somewhere"}],
            "qualifications": [],
            "work_experience": [],  # GT requires >= 3
            "articleship_internships": [],
        }

        passed, failures = ev.compare_with_golden_file(broken_record, golden)

        assert not passed, "Harness should have detected the broken extraction but didn't"
        assert any("full_name" in f for f in failures), \
            f"Expected full_name failure, got: {failures}"
        assert any("work_experience" in f for f in failures), \
            f"Expected work_experience failure, got: {failures}"
