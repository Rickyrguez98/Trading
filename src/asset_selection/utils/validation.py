"""Small validators used by providers and scorers."""
from __future__ import annotations

import math
import re
from typing import Any, Optional

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def is_valid_ticker(ticker: Any, min_len: int = 1, max_len: int = 5) -> bool:
    """A liberal sanity check: uppercase letters/digits, allowed punctuation,
    length within bounds. We let provider-level filters do the heavy lifting
    (ETF / warrant / unit exclusion etc.).
    """
    if not isinstance(ticker, str):
        return False
    t = ticker.strip().upper()
    if not (min_len <= len(t) <= max_len + 2):  # allow .A / .B suffixes
        return False
    return bool(_TICKER_RE.match(t))


def is_finite_number(x: Any) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v)


def coerce_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    """Return a finite float or ``default``. None / NaN / inf collapse to default."""
    if x is None:
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v
