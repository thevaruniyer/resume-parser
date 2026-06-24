"""
Date normalization for resume dates.

Output conventions:
  YYYY-MM  — when month + year are both present
  YYYY     — when only year is available (common in Indian education sections)
  None     — when the value means "current" (Present, Till date, etc.)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_PRESENT_TOKENS = frozenset({
    "present", "current", "till date", "tilldate", "till-date",
    "ongoing", "to date", "todate", "now", "till", "date",
})

_MONTH_MAP: dict[str, int] = {
    "january": 1,  "jan": 1,
    "february": 2, "feb": 2,
    "march": 3,    "mar": 3,
    "april": 4,    "apr": 4,
    "may": 5,
    "june": 6,     "jun": 6,
    "july": 7,     "jul": 7,
    "august": 8,   "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def normalize_date(s: Optional[str]) -> Optional[str]:
    """
    Normalize a resume date string → YYYY-MM, YYYY, or None.

    None is returned for null input or any "present / current" token.
    """
    if s is None:
        return None
    cleaned = s.strip()
    if not cleaned:
        return None

    # Present-type token (strip punctuation before comparing)
    lower = re.sub(r"[.\-]", " ", cleaned.lower()).strip()
    lower_compact = lower.replace(" ", "")
    if lower in _PRESENT_TOKENS or lower_compact in _PRESENT_TOKENS:
        return None

    # YYYY-MM (ISO)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", cleaned)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"

    # MM/YYYY or MM-YYYY  (month first, common in Indian resumes)
    m = re.fullmatch(r"(\d{1,2})[/\-](\d{4})", cleaned)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"

    # "Jan 2020" / "January 2020"
    m = re.fullmatch(r"([A-Za-z]+)[,\s]+(\d{4})", cleaned)
    if m:
        mo = _MONTH_MAP.get(m.group(1).lower())
        y = int(m.group(2))
        if mo and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"

    # "2020 Jan" / "2020 January"
    m = re.fullmatch(r"(\d{4})[,\s]+([A-Za-z]+)", cleaned)
    if m:
        y = int(m.group(1))
        mo = _MONTH_MAP.get(m.group(2).lower())
        if mo and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}"

    # Year-only YYYY
    m = re.fullmatch(r"(\d{4})", cleaned)
    if m:
        y = int(m.group(1))
        if 1900 <= y <= 2100:
            return str(y)

    # Cannot parse — pass through unchanged to avoid data loss
    return cleaned


def _to_ym(s: str) -> tuple[int, int]:
    """Parse YYYY-MM or YYYY to (year, month). Year-only → month 1."""
    if re.fullmatch(r"\d{4}-\d{2}", s):
        y, mo = s.split("-")
        return int(y), int(mo)
    if re.fullmatch(r"\d{4}", s):
        return int(s), 1
    raise ValueError(f"Unparseable date for duration: {s!r}")


def compute_duration_months(
    start: Optional[str],
    end: Optional[str],
    *,
    use_today_for_open: bool = True,
) -> Optional[int]:
    """
    Whole months between start and end (both YYYY-MM or YYYY).

    end=None means the role is current → uses today's date if
    use_today_for_open is True; otherwise returns None.
    """
    if not start:
        return None
    try:
        sy, sm = _to_ym(start)
        if end is None:
            if not use_today_for_open:
                return None
            today = date.today()
            ey, em = today.year, today.month
        else:
            ey, em = _to_ym(end)
        return max(0, (ey - sy) * 12 + (em - sm))
    except (ValueError, TypeError):
        return None
