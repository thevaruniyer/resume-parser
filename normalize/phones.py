"""
Indian phone number normalization.

Target format: +91-XXXXX-XXXXX (10 digits, grouped 5+5).
Handles: +91-98765-43210, 9876543210, 09876543210, +919876543210, 98765 43210, etc.
Non-Indian / ambiguous numbers returned with minimal cleaning (no +91 prefix added).
"""
from __future__ import annotations

import re
from typing import Optional


def _is_india(country: Optional[str]) -> bool:
    """Return True if country indicates India, or if no country context is given."""
    if not country:
        return True  # no country info → assume India (this is an Indian CA pipeline)
    c = country.upper().strip()
    return "INDIA" in c or c in ("IN", "IND")


def normalize_phone(s: Optional[str], country: Optional[str] = None) -> Optional[str]:
    """
    Normalize a phone string.

    Applies +91-XXXXX-XXXXX formatting only when the number looks like an Indian
    mobile (10 digits starting with 6-9) AND the candidate's country is India/IN
    or is not specified.

    For all other cases, returns digits-only (with leading + if original had one).
    Returns None for None/empty input.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None

    had_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)

    if not digits:
        return None

    # 10 raw digits: Indian mobile candidate if starts with 6-9
    if len(digits) == 10:
        if digits[0] in "6789" and _is_india(country):
            return f"+91-{digits[:5]}-{digits[5:]}"
        return digits  # non-Indian or non-Indian format → digits only

    # 11 digits starting with 0: STD-prefix Indian mobile
    if len(digits) == 11 and digits.startswith("0"):
        core = digits[1:]
        if core[0] in "6789" and _is_india(country):
            return f"+91-{core[:5]}-{core[5:]}"
        return digits

    # 12 digits starting with 91: already country-code prefixed → always Indian
    if len(digits) == 12 and digits.startswith("91"):
        core = digits[2:]
        return f"+91-{core[:5]}-{core[5:]}"

    # Unrecognised length — return cleaned
    return ("+" if had_plus else "") + digits


def phone_digit_sequence(phone: Optional[str]) -> Optional[str]:
    """Extract the 10-digit core from a normalized Indian phone number, for comparisons."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 10:
        return digits
    return None
