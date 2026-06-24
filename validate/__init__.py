"""
Phase 3: validate_record — schema rules, confidence scoring, hallucination guard.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

REVIEW_THRESHOLD = 0.70

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Weighted importance for overall_confidence computation
_FIELD_WEIGHTS: dict[str, float] = {
    "full_name": 3.0,
    "emails": 2.5,
    "phones": 2.0,
    "qualifications": 2.0,
    "work_experience": 1.5,
    "education": 1.5,
    "date_of_birth": 1.0,
}


def _validate_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def _validate_phone(phone: str) -> bool:
    """Accept +91-XXXXX-XXXXX (normalized Indian) or 7–15-digit international."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("91") and len(digits) == 12:
        return True
    if len(digits) == 10:
        return True
    if 7 <= len(digits) <= 15:
        return True
    return False


def _dates_ordered(start: Optional[str], end: Optional[str]) -> bool:
    """Return False if end is strictly before start (lexicographic on YYYY-MM or YYYY)."""
    if not start or not end:
        return True
    return end >= start


def _in_source(value: str, source_text: str) -> bool:
    """Case-insensitive substring membership check."""
    if not value or not source_text:
        return True
    return value.lower() in source_text.lower()


def _phone_core_in_source(phone: str, source_text: str) -> bool:
    """Check that the 10-digit core of the phone appears verbatim in source_text."""
    if not source_text:
        return True
    digits = re.sub(r"\D", "", phone)
    core = digits[2:] if (len(digits) == 12 and digits.startswith("91")) else digits
    return core in source_text if core else True


def _base_conf(path_taken: Optional[str]) -> float:
    if path_taken == "text":
        return 1.0
    if path_taken == "hybrid":
        return 0.85
    return 0.8  # vision or unknown


def validate_record(record: dict, source_text: Optional[str] = None) -> dict:
    """
    Run validation rules + confidence scoring. Returns updated record dict.
    Updates: meta.field_confidences, meta.overall_confidence,
             meta.needs_review, meta.review_reasons.
    """
    meta = dict(record.get("meta") or {})
    review_reasons: list[str] = list(meta.get("review_reasons") or [])
    field_confs: dict[str, float] = {}

    path_taken = meta.get("path_taken")
    base = _base_conf(path_taken)
    text_path = path_taken == "text"

    # --- full_name (required) ---
    full_name = record.get("full_name")
    if not full_name or not str(full_name).strip():
        review_reasons.append("full_name_missing")
        field_confs["full_name"] = 0.0
    else:
        c = base
        if text_path and source_text and not _in_source(str(full_name), source_text):
            c = 0.3
            review_reasons.append("hallucination_suspect:full_name")
        field_confs["full_name"] = c

    # --- emails ---
    emails = record.get("emails") or []
    if emails:
        confs = []
        for email in emails:
            if not _validate_email(str(email)):
                review_reasons.append(f"invalid_email:{email}")
                confs.append(0.5)
            else:
                c = base
                if text_path and source_text and not _in_source(str(email), source_text):
                    c = 0.3
                    review_reasons.append(f"hallucination_suspect:email:{email}")
                confs.append(c)
        field_confs["emails"] = sum(confs) / len(confs)
    else:
        field_confs["emails"] = base

    # --- phones ---
    phones = record.get("phones") or []
    if phones:
        confs = []
        for phone in phones:
            if not _validate_phone(str(phone)):
                review_reasons.append(f"invalid_phone:{phone}")
                confs.append(0.5)
            else:
                c = base
                if text_path and source_text and not _phone_core_in_source(str(phone), source_text):
                    c = 0.3
                    review_reasons.append(f"hallucination_suspect:phone:{phone}")
                confs.append(c)
        field_confs["phones"] = sum(confs) / len(confs)
    else:
        field_confs["phones"] = base

    # --- date_of_birth ---
    dob = record.get("date_of_birth")
    if dob:
        c = base
        if text_path and source_text and not _in_source(str(dob), source_text):
            c = 0.3
            review_reasons.append("hallucination_suspect:date_of_birth")
        field_confs["date_of_birth"] = c
    else:
        field_confs["date_of_birth"] = base

    # --- qualifications: spot-check membership numbers via hallucination guard ---
    quals = record.get("qualifications") or []
    if quals:
        conf = base
        for q in quals:
            if not isinstance(q, dict):
                continue
            mn = q.get("membership_number")
            if mn and text_path and source_text and not _in_source(str(mn), source_text):
                conf = min(conf, 0.3)
                review_reasons.append(f"hallucination_suspect:membership_number:{mn}")
        field_confs["qualifications"] = conf
    else:
        field_confs["qualifications"] = base

    # --- work_experience: date order sanity + future start dates ---
    work_exp = record.get("work_experience") or []
    we_conf = base
    today_str = date.today().strftime("%Y-%m")
    for we in work_exp:
        if not isinstance(we, dict):
            continue
        sd, ed = we.get("start_date"), we.get("end_date")
        if not _dates_ordered(sd, ed):
            review_reasons.append(
                f"date_order_invalid:work_experience:{we.get('company', '?')}"
            )
            we_conf = min(we_conf, 0.5)
        if sd and sd > today_str:
            review_reasons.append(f"future_start_date:work_experience:{sd}")
            we_conf = min(we_conf, 0.5)
    field_confs["work_experience"] = we_conf

    # --- education: date order sanity ---
    education = record.get("education") or []
    edu_conf = base
    for edu in education:
        if not isinstance(edu, dict):
            continue
        if not _dates_ordered(edu.get("start_date"), edu.get("end_date")):
            review_reasons.append(
                f"date_order_invalid:education:{edu.get('degree', '?')}"
            )
            edu_conf = min(edu_conf, 0.5)
    field_confs["education"] = edu_conf

    # --- overall confidence (weighted mean over key fields) ---
    total_w = sum(_FIELD_WEIGHTS[f] for f in _FIELD_WEIGHTS if f in field_confs)
    weighted_sum = sum(field_confs[f] * _FIELD_WEIGHTS[f] for f in _FIELD_WEIGHTS if f in field_confs)
    overall = round(weighted_sum / total_w, 4) if total_w > 0 else base

    meta["field_confidences"] = field_confs
    meta["overall_confidence"] = overall
    meta["needs_review"] = overall < REVIEW_THRESHOLD
    meta["review_reasons"] = review_reasons

    result = dict(record)
    result["meta"] = meta
    return result
