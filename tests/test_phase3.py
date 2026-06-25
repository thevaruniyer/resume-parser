"""
Phase 3 tests — Normalize / validate / score.

DoD:
  [x] Date normalization: ≥8 formats → YYYY-MM; Present/Till date → None
  [x] Duration computation: known date ranges → correct duration_months + total_experience_years
  [x] Hallucination guard: injected phone not in source text → flagged, field confidence drops
  [x] Malformed email → validation fails (review_reason added)
  [x] Malformed phone → validation fails (review_reason added)
  [x] Low-confidence record → needs_review=True, review_reasons non-empty
  [x] Dedup key: same email, different names → identical key
  [x] Dedup key: no email, same phone → identical key
  [x] DEAD_LETTER record → normalize_record returns it unchanged
  [x] Ground-truth round-trip: total_experience_years positive, ICAI membership_number preserved

All tests are pure Python — zero Gemini API calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

GT_FILE = ROOT / "ground_truth" / "sample_ca_india_synthetic.json"

from normalize import normalize_record
from normalize.dates import normalize_date, compute_duration_months
from normalize.dedup import compute_dedup_key
from validate import validate_record, REVIEW_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_meta(path_taken: str = "text") -> dict:
    return {
        "source_file": "test.txt",
        "source_path": "test.txt",
        "file_type": "txt",
        "parse_timestamp": "2024-01-01T00:00:00Z",
        "path_taken": path_taken,
        "needs_review": False,
        "review_reasons": [],
    }


def _minimal_record(**kwargs) -> dict:
    r: dict = {
        "full_name": "Test User",
        "emails": ["test@example.com"],
        "phones": [],
        "meta": _minimal_meta(),
    }
    r.update(kwargs)
    return r


# ---------------------------------------------------------------------------
# Date normalization table
# ---------------------------------------------------------------------------

class TestDateNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("Jan 2020",       "2020-01"),
        ("January 2020",   "2020-01"),
        ("01/2020",        "2020-01"),
        ("2020-01",        "2020-01"),
        ("2020-1",         "2020-01"),
        ("11/2023",        "2023-11"),
        ("Nov 2023",       "2023-11"),
        ("2023 Nov",       "2023-11"),
        ("2020",           "2020"),
    ])
    def test_formats(self, raw, expected):
        assert normalize_date(raw) == expected

    @pytest.mark.parametrize("raw", [
        "Present", "present", "Current", "current",
        "Till date", "Till Date", "TILL DATE",
        "Ongoing", "ongoing",
    ])
    def test_present_synonyms_return_none(self, raw):
        assert normalize_date(raw) is None

    def test_none_input_returns_none(self):
        assert normalize_date(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_date("") is None


# ---------------------------------------------------------------------------
# Duration computation
# ---------------------------------------------------------------------------

class TestDurationComputation:
    def test_full_months_between_known_dates(self):
        # 2021-01 → 2023-12 = 35 months
        result = compute_duration_months("2021-01", "2023-12")
        assert result == 35

    def test_year_only_dates(self):
        # 2017 → 2020 treated as 2017-01 → 2020-01 = 36 months
        result = compute_duration_months("2017", "2020")
        assert result == 36

    def test_same_start_end_is_zero(self):
        assert compute_duration_months("2022-06", "2022-06") == 0

    def test_no_start_returns_none(self):
        assert compute_duration_months(None, "2023-01") is None

    def test_open_end_returns_positive(self):
        result = compute_duration_months("2020-01", None)
        assert result is not None and result > 0


class TestTotalExperienceYears:
    def test_single_role_positive(self):
        record = _minimal_record(
            work_experience=[{
                "company": "Acme",
                "designation": "Analyst",
                "start_date": "2021-01",
                "end_date": "2023-12",
                "is_current": False,
            }]
        )
        result = normalize_record(record)
        assert result["derived"]["total_experience_years"] == pytest.approx(2.9, abs=0.1)

    def test_overlapping_roles_deduped(self):
        # Two overlapping intervals: 2020-01–2022-01 and 2021-01–2023-01
        # Merged: 2020-01–2023-01 = 36 months
        record = _minimal_record(
            work_experience=[
                {"company": "A", "start_date": "2020-01", "end_date": "2022-01", "is_current": False},
                {"company": "B", "start_date": "2021-01", "end_date": "2023-01", "is_current": False},
            ]
        )
        result = normalize_record(record)
        assert result["derived"]["total_experience_years"] == pytest.approx(3.0, abs=0.1)

    def test_no_work_experience_gives_none(self):
        record = _minimal_record(work_experience=[])
        result = normalize_record(record)
        assert result["derived"]["total_experience_years"] is None


# ---------------------------------------------------------------------------
# Hallucination guard
# ---------------------------------------------------------------------------

class TestHallucinationGuard:
    def test_phone_not_in_source_text_is_flagged(self):
        source = "Rahul Mehta, email: rahul.mehta@example.com. No phone."
        record = _minimal_record(
            full_name="Rahul Mehta",
            emails=["rahul.mehta@example.com"],
            phones=["+91-99999-99999"],  # digits 9999999999 absent from source
            meta=_minimal_meta("text"),
        )
        result = validate_record(record, source_text=source)
        assert any("phone" in r for r in result["meta"]["review_reasons"])
        assert result["meta"]["field_confidences"]["phones"] == pytest.approx(0.3)

    def test_field_in_source_text_not_flagged(self):
        source = "Rahul Mehta, 9876543210, rahul@example.com"
        record = _minimal_record(
            full_name="Rahul Mehta",
            emails=["rahul@example.com"],
            phones=["+91-98765-43210"],
            meta=_minimal_meta("text"),
        )
        result = validate_record(record, source_text=source)
        assert not any("hallucination" in r for r in result["meta"]["review_reasons"])
        assert result["meta"]["field_confidences"]["phones"] == pytest.approx(1.0)

    def test_vision_path_skips_hallucination_guard(self):
        # Vision path: no source text available, guard must not fire
        source = "Rahul Mehta, email: rahul@example.com"
        record = _minimal_record(
            phones=["+91-99999-99999"],
            meta=_minimal_meta("vision"),
        )
        result = validate_record(record, source_text=source)
        assert not any("hallucination" in r for r in result["meta"]["review_reasons"])


# ---------------------------------------------------------------------------
# Validation: email + phone format
# ---------------------------------------------------------------------------

class TestEmailValidation:
    def test_malformed_email_adds_review_reason(self):
        record = _minimal_record(emails=["not-an-email"])
        result = validate_record(record)
        assert any("invalid_email" in r for r in result["meta"]["review_reasons"])

    def test_no_at_sign_fails(self):
        record = _minimal_record(emails=["nodomain.com"])
        result = validate_record(record)
        assert any("invalid_email" in r for r in result["meta"]["review_reasons"])

    def test_no_dot_after_at_fails(self):
        record = _minimal_record(emails=["user@nodot"])
        result = validate_record(record)
        assert any("invalid_email" in r for r in result["meta"]["review_reasons"])

    def test_valid_email_passes(self):
        record = _minimal_record(emails=["good@example.com"])
        result = validate_record(record)
        assert not any("invalid_email" in r for r in result["meta"]["review_reasons"])


class TestPhoneValidation:
    def test_too_short_phone_fails(self):
        record = _minimal_record(emails=[], phones=["12345"])
        result = validate_record(record)
        assert any("invalid_phone" in r for r in result["meta"]["review_reasons"])

    def test_normalized_indian_phone_passes(self):
        record = _minimal_record(phones=["+91-98765-43210"])
        result = validate_record(record)
        assert not any("invalid_phone" in r for r in result["meta"]["review_reasons"])

    def test_ten_digit_phone_passes(self):
        record = _minimal_record(phones=["9876543210"])
        result = validate_record(record)
        assert not any("invalid_phone" in r for r in result["meta"]["review_reasons"])


class TestPhoneNormalization:
    def test_us_number_does_not_get_india_prefix(self):
        from normalize.phones import normalize_phone
        result = normalize_phone("+0 (000) 111 1111", country="United States")
        assert result is not None
        assert not result.startswith("+91")

    def test_indian_mobile_no_country_gets_prefix(self):
        from normalize.phones import normalize_phone
        assert normalize_phone("9876543210") == "+91-98765-43210"

    def test_indian_mobile_with_india_country_gets_prefix(self):
        from normalize.phones import normalize_phone
        assert normalize_phone("9876543210", country="India") == "+91-98765-43210"

    def test_indian_mobile_with_us_country_no_prefix(self):
        from normalize.phones import normalize_phone
        result = normalize_phone("9876543210", country="United States")
        assert result == "9876543210"  # digits only, no +91

    def test_number_starting_5_no_country_no_prefix(self):
        from normalize.phones import normalize_phone
        result = normalize_phone("5123456789")
        assert result == "5123456789"  # 5 is not a valid Indian mobile prefix


# ---------------------------------------------------------------------------
# Low-confidence → needs_review
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    def test_low_confidence_sets_needs_review(self):
        # Missing full_name (weight 3.0 → conf 0.0) + invalid email + bad phone
        record = {
            "full_name": None,
            "emails": ["not-an-email"],
            "phones": ["123"],
            "meta": _minimal_meta("text"),
        }
        result = validate_record(record)
        assert result["meta"]["needs_review"] is True
        assert len(result["meta"]["review_reasons"]) > 0
        assert result["meta"]["overall_confidence"] < REVIEW_THRESHOLD

    def test_clean_record_not_flagged(self):
        source = "Test User test@example.com 9876543210"
        record = _minimal_record(phones=["+91-98765-43210"], meta=_minimal_meta("text"))
        result = validate_record(record, source_text=source)
        assert result["meta"]["needs_review"] is False
        assert result["meta"]["overall_confidence"] >= REVIEW_THRESHOLD

    def test_overall_confidence_is_in_range(self):
        record = _minimal_record()
        result = validate_record(record)
        oc = result["meta"]["overall_confidence"]
        assert 0.0 <= oc <= 1.0

    def test_vision_path_base_confidence_is_0_8(self):
        record = _minimal_record(meta=_minimal_meta("vision"))
        result = validate_record(record)
        # All field confidences should be 0.8 (base for vision)
        for v in result["meta"]["field_confidences"].values():
            assert v == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Dedup key
# ---------------------------------------------------------------------------

class TestDedupKey:
    def test_same_email_different_names_same_key(self):
        k1 = compute_dedup_key(["alice@example.com"], [], "Alice Smith")
        k2 = compute_dedup_key(["alice@example.com"], [], "Alice Jones")
        assert k1 == k2

    def test_same_phone_no_email_same_key(self):
        k1 = compute_dedup_key([], ["9876543210"], "Alice Smith")
        k2 = compute_dedup_key([], ["9876543210"], "Bob Jones")
        assert k1 == k2

    def test_different_emails_different_keys(self):
        k1 = compute_dedup_key(["alice@example.com"], [])
        k2 = compute_dedup_key(["bob@example.com"], [])
        assert k1 != k2

    def test_email_takes_priority_over_phone(self):
        # If both email and phone present, email wins; same email → same key
        k1 = compute_dedup_key(["x@example.com"], ["1111111111"])
        k2 = compute_dedup_key(["x@example.com"], ["2222222222"])
        assert k1 == k2

    def test_no_identifiers_returns_unknown_hash(self):
        k = compute_dedup_key([], [], None, None)
        import hashlib
        assert k == hashlib.sha1(b"unknown").hexdigest()

    def test_dedup_key_added_to_meta_by_normalize(self):
        record = _minimal_record(emails=["test@example.com"])
        result = normalize_record(record)
        assert "dedup_key" in result["meta"]
        assert len(result["meta"]["dedup_key"]) == 40  # SHA1 hex length


# ---------------------------------------------------------------------------
# DEAD_LETTER pass-through
# ---------------------------------------------------------------------------

class TestDeadLetterPassThrough:
    def test_dead_letter_uppercase_unchanged(self):
        record = {
            "full_name": "Test",
            "meta": {
                "source_file": "bad.doc",
                "source_path": "bad.doc",
                "file_type": "doc",
                "parse_timestamp": "2024-01-01T00:00:00Z",
                "path_taken": "DEAD_LETTER",
                "needs_review": True,
                "review_reasons": ["unsupported_format"],
            },
        }
        result = normalize_record(record)
        assert result is record  # exact same object, no copy

    def test_dead_letter_lowercase_unchanged(self):
        record = {
            "full_name": "Test",
            "meta": {"path_taken": "dead_letter", "source_file": "x", "source_path": "x", "file_type": "doc", "parse_timestamp": "t"},
        }
        result = normalize_record(record)
        assert result is record

    def test_dead_letter_has_no_dedup_key(self):
        record = {
            "emails": ["test@test.com"],
            "meta": {"path_taken": "DEAD_LETTER", "source_file": "x", "source_path": "x", "file_type": "doc", "parse_timestamp": "t"},
        }
        result = normalize_record(record)
        assert "dedup_key" not in result.get("meta", {})


# ---------------------------------------------------------------------------
# Ground-truth round-trip
# ---------------------------------------------------------------------------

class TestGroundTruthRoundTrip:
    def setup_method(self):
        with open(GT_FILE) as f:
            raw = json.load(f)
        raw.pop("_notes", None)
        # Ground truth meta is minimal; fill required path_taken for normalization
        raw.setdefault("meta", {})
        raw["meta"].setdefault("path_taken", "text")
        self.record = raw

    def test_total_experience_years_positive(self):
        result = normalize_record(self.record)
        tey = result["derived"]["total_experience_years"]
        assert tey is not None
        assert tey > 0

    def test_icai_membership_number_preserved(self):
        result = normalize_record(self.record)
        qualifications = result.get("qualifications") or []
        found = any(
            q.get("membership_number") == "123456"
            for q in qualifications
            if isinstance(q, dict)
        )
        assert found, "ICAI membership number 123456 must survive normalization"

    def test_work_experience_duration_computed(self):
        result = normalize_record(self.record)
        # Deloitte articleship: 2021-01 → 2023-12 = 35 months
        we = result["work_experience"]
        assert len(we) >= 1
        assert we[0]["duration_months"] == 35

    def test_phones_normalized_to_e164_style(self):
        result = normalize_record(self.record)
        for phone in result.get("phones") or []:
            assert phone.startswith("+91-")

    def test_validate_after_normalize_schema_valid(self):
        normalized = normalize_record(self.record)
        validated = validate_record(normalized)
        meta = validated["meta"]
        assert "overall_confidence" in meta
        assert "field_confidences" in meta
        assert isinstance(meta["needs_review"], bool)

    def test_dedup_key_present_and_sha1_length(self):
        normalized = normalize_record(self.record)
        assert "dedup_key" in normalized["meta"]
        assert len(normalized["meta"]["dedup_key"]) == 40
