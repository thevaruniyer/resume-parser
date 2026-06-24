"""
Indian phone number normalization.

Target format: +91-XXXXX-XXXXX (10 digits, grouped 5+5).
Handles: +91-98765-43210, 9876543210, 09876543210, +919876543210, 98765 43210, etc.
Non-Indian / ambiguous numbers returned with minimal cleaning (no +91 prefix added).
"""
from __future__ import annotations

import re
from typing import Optional


def normalize_phone(s: Optional[str]) -> Optional[str]:
    """
    Normalize a phone string to +91-XXXXX-XXXXX if it looks Indian.

    Returns None for None/empty input.
    Returns cleaned (digits only with + if original had +) for non-Indian formats.
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

    # 10 raw digits → Indian mobile (no country code)
    if len(digits) == 10:
        return f"+91-{digits[:5]}-{digits[5:]}"

    # 11 digits starting with 0 → STD-prefix Indian mobile
    if len(digits) == 11 and digits.startswith("0"):
        core = digits[1:]
        return f"+91-{core[:5]}-{core[5:]}"

    # 12 digits starting with 91 → country-code prefixed
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
