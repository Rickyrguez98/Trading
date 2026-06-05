"""Growth metric extraction.

The growth pillar measures how fast the business is expanding. Higher values
are *better* — we keep the natural sign here; sign flipping for "lower is
better" metrics happens in the scorer.
"""
from __future__ import annotations

from typing import Dict, Optional

from ..data_providers.base import Fundamentals

GROWTH_METRICS = ("revenue_growth", "earnings_growth", "fcf_growth")


def extract_growth_metrics(f: Fundamentals) -> Dict[str, Optional[float]]:
    return {name: getattr(f, name, None) for name in GROWTH_METRICS}
