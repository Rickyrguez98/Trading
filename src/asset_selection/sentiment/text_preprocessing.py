"""Minimal text cleanup before scoring.

VADER works on raw English text, so we deliberately keep cleanup light:
collapse whitespace, strip HTML-ish residue, and trim. Heavy normalization
(removing tickers, lowercasing) would degrade VADER's accuracy.
"""
from __future__ import annotations

import re

_HTML_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    if not text:
        return ""
    out = _HTML_RE.sub(" ", text)
    out = _WHITESPACE_RE.sub(" ", out).strip()
    return out
