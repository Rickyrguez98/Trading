"""Quality metric extraction.

Profitability and capital-efficiency metrics; higher is better.
"""
from __future__ import annotations

from typing import Dict, Optional

from ..data_providers.base import Fundamentals

QUALITY_METRICS = ("roe", "roa", "operating_margin", "net_margin")


def extract_quality_metrics(f: Fundamentals) -> Dict[str, Optional[float]]:
    return {name: getattr(f, name, None) for name in QUALITY_METRICS}
