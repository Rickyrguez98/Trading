"""Valuation metric extraction.

For these metrics *lower is better* (cheaper). The sign flip happens in the
scorer (``invert_in_scoring=True``) so that the raw numbers stored on the
DataFrame remain the standard quoted values.
"""
from __future__ import annotations

from typing import Dict, Optional

from ..data_providers.base import Fundamentals

VALUATION_METRICS = ("pe_ratio", "forward_pe", "peg_ratio", "price_to_sales", "price_to_book")


def extract_valuation_metrics(f: Fundamentals) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for name in VALUATION_METRICS:
        value = getattr(f, name, None)
        # Negative ratios are mathematically meaningless here (e.g. negative P/E
        # comes from negative earnings -> we mark missing so the scorer doesn't
        # reward it as "cheap").
        if value is not None and value <= 0:
            out[name] = None
        else:
            out[name] = value
    return out
