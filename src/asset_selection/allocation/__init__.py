"""Portfolio allocation — turns the selection ranking into a sized portfolio.

Consumes ``asset_selection_results.csv`` and produces three risk strategies
(conservative / balanced / aggressive). Each strategy is a **sleeve budget**
over cash, a bond proxy, a broad index core, quality single names, growth
satellites and an opt-in, hard-capped speculative sleeve; the equity sleeves
are filled from the ranking, weighted by the score selection already computed
(fundamentals + news + risk) with an inverse-volatility tilt, then constrained
by per-name and portfolio-wide per-sector caps.

Research output only: it reports percentages, dollar amounts and share counts
for a capital amount you supply. It places no orders and is not advice.
See docs/us_equities/allocation-readiness.md and docs/us_equities/allocation.md.
"""
from .config import (  # noqa: F401
    SLEEVES,
    PortfolioConfig,
    PortfolioConfigError,
    StrategyConfig,
    load_config,
    validate_config,
)
from .engine import build_plan, eligible_candidates, raw_weights  # noqa: F401
from .models import AllocationResult, Position, SleeveResult, StrategyPlan  # noqa: F401
from .money import finalize_plan, order_rows  # noqa: F401

__all__ = [
    "PortfolioConfig",
    "PortfolioConfigError",
    "StrategyConfig",
    "SLEEVES",
    "load_config",
    "validate_config",
    "build_plan",
    "eligible_candidates",
    "raw_weights",
    "finalize_plan",
    "order_rows",
    "AllocationResult",
    "Position",
    "SleeveResult",
    "StrategyPlan",
]
