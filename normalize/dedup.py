"""
Dedup key generation.

Priority:
  1. Lowercased email (most reliable cross-record identifier)
  2. 10-digit phone digits (fallback when no email)
  3. Normalised name + DOB string (last resort)
  4. Literal "unknown" (no identifying information at all)

The raw key string is SHA1-hashed to a fixed-length hex string stored in
meta.dedup_key.  SHA1 is used for speed and compactness, not security —
the key is a stable identifier, not a secret.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional


def _email_key(emails: list) -> Optional[str]:
    for e in emails or []:
        if e and "@" in str(e):
            return str(e).strip().lower()
    return None


def _phone_key(phones: list) -> Optional[str]:
    for p in phones or []:
        digits = re.sub(r"\D", "", str(p or ""))
        # Prefer 10-digit core
        if len(digits) == 10:
            return digits
        if len(digits) == 12 and digits.startswith("91"):
            return digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            return digits[1:]
    return None


def _name_dob_key(full_name: Optional[str], dob: Optional[str]) -> Optional[str]:
    if not full_name:
        return None
    name_norm = re.sub(r"\s+", " ", str(full_name).strip().lower())
    dob_norm = str(dob or "").strip()
    return f"{name_norm}|{dob_norm}"


def compute_dedup_key(
    emails: list,
    phones: list,
    full_name: Optional[str] = None,
    date_of_birth: Optional[str] = None,
) -> str:
    """
    Compute a stable dedup key and return its SHA1 hex digest.

    The raw key used for hashing follows the priority chain:
      email → phone-digits → name|dob → "unknown"
    """
    raw = (
        _email_key(emails)
        or _phone_key(phones)
        or _name_dob_key(full_name, date_of_birth)
        or "unknown"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
