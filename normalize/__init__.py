"""
Phase 3: normalize_record — dates, durations, phones, dedup key.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any, Optional

from normalize.dates import normalize_date, compute_duration_months, _to_ym
from normalize.phones import normalize_phone
from normalize.dedup import compute_dedup_key

_DEAD_LETTER_PATHS = {"DEAD_LETTER", "dead_letter"}

_PRESENT_RE = re.compile(
    r"^(present|current|till\s*date|ongoing|to\s*date|now|till)$", re.IGNORECASE
)


def _is_present_str(s: Optional[str]) -> bool:
    """Return True if s signals 'still ongoing' (present/current/till date/None)."""
    if s is None:
        return True
    cleaned = re.sub(r"[.\-]", " ", s.strip())
    return bool(_PRESENT_RE.match(cleaned.strip()))


def _strip_strings(obj: Any) -> Any:
    """Recursively strip whitespace from all string values in a nested structure."""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        return {k: _strip_strings(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_strings(item) for item in obj]
    return obj


def _normalize_entry_dates(
    entry: dict,
    start_key: str = "start_date",
    end_key: str = "end_date",
) -> tuple[dict, bool]:
    """
    Normalize start/end dates inside a dict entry.
    Returns (updated_entry, is_open_ended).
    is_open_ended=True when end_date was None or a "present" synonym.
    """
    entry = dict(entry)
    entry[start_key] = normalize_date(entry.get(start_key))
    end_raw = entry.get(end_key)
    open_ended = _is_present_str(end_raw)
    if open_ended:
        entry[end_key] = None
    else:
        entry[end_key] = normalize_date(end_raw)
    return entry, open_ended


def _total_experience_months(work_experiences: list[dict]) -> int:
    """Sum non-overlapping work-experience intervals in months."""
    intervals: list[tuple[int, int]] = []
    for we in work_experiences:
        start = we.get("start_date")
        end = we.get("end_date")
        if not start:
            continue
        try:
            sy, sm = _to_ym(start)
            if end is None:
                today = date.today()
                ey, em = today.year, today.month
            else:
                ey, em = _to_ym(end)
            start_m = sy * 12 + sm
            end_m = ey * 12 + em
            if end_m > start_m:
                intervals.append((start_m, end_m))
        except (ValueError, TypeError):
            continue

    if not intervals:
        return 0

    intervals.sort()
    merged: list[list[int]] = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    return sum(e - s for s, e in merged)


def normalize_record(record: dict) -> dict:
    """
    Run all normalization on a parsed record dict. Returns new dict.
    DEAD_LETTER records pass through untouched (same reference returned).
    """
    meta = record.get("meta") or {}
    if meta.get("path_taken") in _DEAD_LETTER_PATHS:
        return record

    record = copy.deepcopy(record)
    record = _strip_strings(record)

    # Phones: normalize to +91-XXXXX-XXXXX for Indian numbers only
    phones = record.get("phones") or []
    country = (record.get("location") or {}).get("country") or ""
    record["phones"] = [normalize_phone(p, country=country) for p in phones if p]

    # Work experience: normalize dates, derive duration + is_current
    current_employer: Optional[str] = None
    current_designation: Optional[str] = None
    work_exp = record.get("work_experience") or []
    for i, entry in enumerate(work_exp):
        entry, open_ended = _normalize_entry_dates(entry)
        entry["duration_months"] = compute_duration_months(
            entry.get("start_date"), entry.get("end_date")
        )
        entry["is_current"] = open_ended
        if open_ended and current_employer is None:
            current_employer = entry.get("company")
            current_designation = entry.get("designation")
        work_exp[i] = entry
    record["work_experience"] = work_exp

    # Articleship: normalize dates + duration
    articleship = record.get("articleship_internships") or []
    for i, entry in enumerate(articleship):
        entry, _ = _normalize_entry_dates(entry)
        entry["duration_months"] = compute_duration_months(
            entry.get("start_date"), entry.get("end_date")
        )
        articleship[i] = entry
    record["articleship_internships"] = articleship

    # Education: normalize dates + duration in years
    education = record.get("education") or []
    for i, entry in enumerate(education):
        entry, _ = _normalize_entry_dates(entry)
        dur = compute_duration_months(entry.get("start_date"), entry.get("end_date"))
        entry["duration_years"] = round(dur / 12, 1) if dur is not None else None
        education[i] = entry
    record["education"] = education

    # Derived block
    total_months = _total_experience_months(record.get("work_experience") or [])
    total_exp_years = round(total_months / 12, 1) if total_months > 0 else None

    derived = dict(record.get("derived") or {})
    derived["total_experience_years"] = total_exp_years
    if current_employer is not None:
        derived["current_employer"] = current_employer
    if current_designation is not None:
        derived["current_designation"] = current_designation
    record["derived"] = derived

    # Dedup key → stored in meta
    dedup_key = compute_dedup_key(
        record.get("emails") or [],
        record.get("phones") or [],
        record.get("full_name"),
        record.get("date_of_birth"),
    )
    if isinstance(record.get("meta"), dict):
        record["meta"]["dedup_key"] = dedup_key

    return record
