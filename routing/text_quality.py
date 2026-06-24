"""
Text-quality heuristics — detect garbled or empty text layers.

Used by the routing cascade to decide whether extracted text is trustworthy
enough for the text path, or whether the page should escalate to vision.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


# A "real word" token: ≥3 consecutive alphabetic characters.
_REAL_WORD_RE = re.compile(r"[A-Za-zऀ-ॿ؀-ۿ]{3,}")

# Characters we consider "junk": not alphanumeric, not common punctuation, not whitespace.
_JUNK_RE = re.compile(r"[^\w\s.,;:!?'\"\-/()\[\]{}@#%&+=<>|\\]", re.UNICODE)


@dataclass
class TextQualityResult:
    ok: bool
    reason: str
    token_count: int
    junk_ratio: float
    space_ratio: float


def text_quality_ok(text: str, min_tokens: int = 30) -> TextQualityResult:
    """
    Return True if `text` looks like genuine prose (not a garbled font export).

    Thresholds (tunable via caller):
      - token_count ≥ min_tokens (default 30 real-word tokens)
      - junk_ratio  < 0.30 (fewer than 30% non-standard chars)
      - space_ratio > 0.02 (at least 2% spaces — catches no-space garble)
    """
    if not text or not text.strip():
        return TextQualityResult(
            ok=False, reason="empty_text",
            token_count=0, junk_ratio=1.0, space_ratio=0.0,
        )

    total_chars = len(text)
    space_count = text.count(" ") + text.count("\t")
    space_ratio = space_count / total_chars if total_chars else 0.0

    # Count junk only over non-whitespace characters
    non_ws = re.sub(r"\s", "", text)
    junk_count = len(_JUNK_RE.findall(non_ws))
    junk_ratio = junk_count / len(non_ws) if non_ws else 1.0

    tokens = _REAL_WORD_RE.findall(text)
    token_count = len(tokens)

    if token_count < min_tokens:
        return TextQualityResult(
            ok=False, reason=f"too_few_tokens:{token_count}<{min_tokens}",
            token_count=token_count, junk_ratio=junk_ratio, space_ratio=space_ratio,
        )
    if junk_ratio >= 0.30:
        return TextQualityResult(
            ok=False, reason=f"high_junk_ratio:{junk_ratio:.2f}",
            token_count=token_count, junk_ratio=junk_ratio, space_ratio=space_ratio,
        )
    if space_ratio < 0.02:
        return TextQualityResult(
            ok=False, reason=f"low_space_ratio:{space_ratio:.3f}",
            token_count=token_count, junk_ratio=junk_ratio, space_ratio=space_ratio,
        )

    return TextQualityResult(
        ok=True, reason="ok",
        token_count=token_count, junk_ratio=junk_ratio, space_ratio=space_ratio,
    )
