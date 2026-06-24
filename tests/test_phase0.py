"""
Phase 0 smoke tests.

DoD checklist:
  [x] env + keys load
  [x] Pydantic schema compiles and validates a sample dict
  [x] test corpus discoverable (test_corpus/files/ has ≥1 file)
  [x] ground-truth labels discoverable (ground_truth/ has ≥1 .json)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CORPUS_FILES = ROOT / "test_corpus" / "files"
GROUND_TRUTH = ROOT / "ground_truth"


# ---------------------------------------------------------------------------
# 1 — env / keys
# ---------------------------------------------------------------------------

class TestKeysLoad:
    def test_gemini_api_key_present(self):
        """GEMINI_API_KEY must be set and non-empty after config loads."""
        from config import settings
        key = settings.gemini_api_key
        assert key, "GEMINI_API_KEY is empty"
        assert len(key) > 10, "GEMINI_API_KEY looks too short to be valid"

    def test_dashscope_api_key_present(self):
        """DASHSCOPE_API_KEY is optional (Qwen fallback). Skip if not yet configured."""
        from config import settings
        key = settings.dashscope_api_key
        if not key:
            pytest.skip("DASHSCOPE_API_KEY not set — add it to .env before using QwenExtractor")
        assert len(key) > 10, "DASHSCOPE_API_KEY looks too short to be valid"

    def test_settings_has_model_names(self):
        from config import settings
        assert settings.gemini_model == "gemini-2.5-flash"
        assert "qwen" in settings.qwen_model.lower()

    def test_confidence_threshold_in_range(self):
        from config import settings
        assert 0.0 < settings.confidence_threshold < 1.0


# ---------------------------------------------------------------------------
# 2 — schema
# ---------------------------------------------------------------------------

class TestSchemaCompiles:
    def test_resume_record_imports(self):
        from schema import ResumeRecord  # noqa: F401

    def test_all_submodels_import(self):
        from schema import (  # noqa: F401
            ArticleshipInternship,
            DerivedFields,
            Education,
            Language,
            Links,
            Location,
            MetaBlock,
            Project,
            Qualification,
            ResumeRecord,
            Skill,
            WorkExperience,
        )

    def test_minimal_record_validates(self):
        """A record with only the mandatory meta block must be valid."""
        from schema import ResumeRecord

        record = ResumeRecord.model_validate({
            "meta": {
                "source_file": "test.txt",
                "source_path": "/tmp/test.txt",
                "file_type": "txt",
                "parse_timestamp": "2026-06-24T00:00:00Z",
            }
        })
        assert record.meta.source_file == "test.txt"
        assert record.full_name is None
        assert record.emails == []
        assert record.education == []

    def test_full_ca_record_validates(self):
        """A full Indian CA record (all list blocks populated) must validate."""
        from schema import ResumeRecord

        data = {
            "full_name": "Rahul Mehta",
            "first_name": "Rahul",
            "last_name": "Mehta",
            "emails": ["rahul.mehta@example.com"],
            "phones": ["+91-98765-43210"],
            "location": {"city": "Mumbai", "state": "Maharashtra", "country": "India"},
            "qualifications": [
                {
                    "name": "CA",
                    "body": "ICAI",
                    "level": "Final",
                    "membership_number": "123456",
                    "attempts": 2,
                    "date_cleared": "2023-11",
                    "status": "cleared",
                }
            ],
            "articleship_internships": [
                {
                    "firm_or_org": "Deloitte Haskins & Sells LLP",
                    "role": "Article Assistant",
                    "start_date": "2021-01",
                    "end_date": "2023-12",
                    "duration_months": 36,
                    "areas": ["Statutory Audit", "GST Compliance", "Direct Taxation"],
                }
            ],
            "education": [
                {
                    "degree": "B.Com (Hons)",
                    "institution": "University of Mumbai",
                    "start_date": "2017",
                    "end_date": "2020",
                    "status": "completed",
                    "grade_type": "percentage",
                    "grade_value": "78",
                }
            ],
            "work_experience": [
                {
                    "company": "Deloitte Haskins & Sells LLP",
                    "designation": "Article Assistant",
                    "employment_type": "intern",
                    "start_date": "2021-01",
                    "end_date": "2023-12",
                    "is_current": False,
                    "responsibilities": ["Conducted statutory audits"],
                }
            ],
            "skills": [
                {"name": "Tally ERP 9", "category": "technical"},
                {"name": "GST", "category": "domain"},
            ],
            "meta": {
                "source_file": "sample_ca_india_synthetic.txt",
                "source_path": "test_corpus/files/sample_ca_india_synthetic.txt",
                "file_type": "txt",
                "parse_timestamp": "2026-06-24T00:00:00Z",
                "overall_confidence": 0.95,
                "needs_review": False,
            },
        }
        record = ResumeRecord.model_validate(data)
        assert record.full_name == "Rahul Mehta"
        assert len(record.qualifications) == 1
        assert record.qualifications[0].membership_number == "123456"
        assert len(record.articleship_internships) == 1
        assert "Statutory Audit" in record.articleship_internships[0].areas
        assert record.meta.overall_confidence == 0.95

    def test_confidence_out_of_range_rejected(self):
        """overall_confidence outside [0,1] must raise ValidationError."""
        from pydantic import ValidationError
        from schema import ResumeRecord

        with pytest.raises(ValidationError):
            ResumeRecord.model_validate({
                "meta": {
                    "source_file": "x.txt",
                    "source_path": "/x.txt",
                    "file_type": "txt",
                    "parse_timestamp": "2026-06-24T00:00:00Z",
                    "overall_confidence": 1.5,
                }
            })

    def test_derived_block_optional(self):
        """derived block is computed downstream — must be fully optional."""
        from schema import ResumeRecord

        record = ResumeRecord.model_validate({
            "meta": {
                "source_file": "x.txt",
                "source_path": "/x.txt",
                "file_type": "txt",
                "parse_timestamp": "2026-06-24T00:00:00Z",
            }
        })
        assert record.derived is None

    def test_multiple_education_entries_preserved(self):
        """All education entries must be captured, not just the latest."""
        from schema import ResumeRecord

        data = {
            "education": [
                {"degree": "SSC", "end_date": "2015", "status": "completed"},
                {"degree": "HSC", "end_date": "2017", "status": "completed"},
                {"degree": "B.Com", "end_date": "2020", "status": "completed"},
            ],
            "meta": {
                "source_file": "x.txt",
                "source_path": "/x.txt",
                "file_type": "txt",
                "parse_timestamp": "2026-06-24T00:00:00Z",
            },
        }
        record = ResumeRecord.model_validate(data)
        assert len(record.education) == 3, "All education entries must be preserved"


# ---------------------------------------------------------------------------
# 3 — corpus discoverable
# ---------------------------------------------------------------------------

class TestCorpusDiscoverable:
    def test_corpus_files_dir_exists(self):
        assert CORPUS_FILES.exists(), f"Missing: {CORPUS_FILES}"
        assert CORPUS_FILES.is_dir()

    def test_corpus_has_files(self):
        files = list(CORPUS_FILES.iterdir())
        assert len(files) >= 1, "test_corpus/files/ is empty"

    def test_corpus_has_text_files(self):
        txt_files = list(CORPUS_FILES.glob("*.txt"))
        assert len(txt_files) >= 1, "No .txt files found in corpus"

    def test_corpus_has_pdf(self):
        pdfs = list(CORPUS_FILES.glob("*.pdf"))
        assert len(pdfs) >= 1, "No .pdf files found in corpus"

    def test_corpus_has_docx(self):
        docx_files = list(CORPUS_FILES.glob("*.docx"))
        assert len(docx_files) >= 1, "No .docx files found in corpus"

    def test_corpus_has_jpg(self):
        jpgs = list(CORPUS_FILES.glob("*.jpg"))
        assert len(jpgs) >= 1, "No .jpg files found in corpus"

    def test_corpus_has_ca_sample(self):
        ca_files = list(CORPUS_FILES.glob("*ca_india*"))
        assert len(ca_files) >= 1, "No Indian CA sample found in corpus"

    def test_corpus_files_are_nonempty(self):
        for f in CORPUS_FILES.iterdir():
            # Intentionally-corrupt fixture is the only allowed zero-byte file
            if "corrupt" in f.name:
                continue
            assert f.stat().st_size > 0, f"Corpus file is empty: {f.name}"


# ---------------------------------------------------------------------------
# 4 — ground-truth labels discoverable
# ---------------------------------------------------------------------------

class TestGroundTruthDiscoverable:
    def test_ground_truth_dir_exists(self):
        assert GROUND_TRUTH.exists(), f"Missing: {GROUND_TRUTH}"
        assert GROUND_TRUTH.is_dir()

    def test_ground_truth_has_labels(self):
        labels = list(GROUND_TRUTH.glob("*.json"))
        assert len(labels) >= 1, "No ground-truth .json files found"

    def test_ground_truth_files_are_valid_json(self):
        for label_path in GROUND_TRUTH.glob("*.json"):
            with open(label_path, encoding="utf-8") as f:
                data = json.load(f)
            assert isinstance(data, dict), f"Label is not a JSON object: {label_path.name}"

    def test_ground_truth_has_ca_label(self):
        ca_labels = list(GROUND_TRUTH.glob("*ca_india*"))
        assert len(ca_labels) >= 1, "No Indian CA ground-truth label found"

    def test_ground_truth_labels_have_meta(self):
        """Content label files must have a meta block (mirrors ResumeRecord contract).

        Phase 2 routing labels (identified by 'expected_routing_path' key) are a
        different schema — they capture routing expectations, not ResumeRecord data.
        """
        for label_path in GROUND_TRUTH.glob("*.json"):
            with open(label_path, encoding="utf-8") as f:
                data = json.load(f)
            if "expected_routing_path" in data:
                continue  # routing label — different schema, no meta required
            assert "meta" in data, f"Missing 'meta' block in {label_path.name}"

    def test_ground_truth_covers_corpus_txt_files(self):
        """Every .txt corpus file should have a corresponding ground-truth label."""
        txt_stems = {f.stem for f in CORPUS_FILES.glob("*.txt")}
        label_stems = {f.stem for f in GROUND_TRUTH.glob("*.json")}
        missing = txt_stems - label_stems
        assert not missing, f"No ground-truth label for: {missing}"


# ---------------------------------------------------------------------------
# 5 — abstract base contracts importable
# ---------------------------------------------------------------------------

class TestBaseContracts:
    def test_storage_connector_importable(self):
        from connectors import FileInfo, StorageConnector  # noqa: F401

    def test_router_importable(self):
        from routing import ExtractionPath, Router, RoutingDecision  # noqa: F401

    def test_extractor_importable(self):
        from extraction import Extractor, ExtractionError  # noqa: F401

    def test_sink_importable(self):
        from output import Sink  # noqa: F401

    def test_extractor_path_enum_values(self):
        from routing import ExtractionPath

        assert ExtractionPath.TEXT == "text"
        assert ExtractionPath.VISION == "vision"
        assert ExtractionPath.HYBRID == "hybrid"
        assert ExtractionPath.DEAD_LETTER == "dead_letter"
