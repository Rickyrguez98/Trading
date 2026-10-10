"""Domain model for an allocation plan.

A plan is deliberately explainable: every position records which sleeve it
came from, why it got the weight it did (score and risk inputs), what was
capped, and how the percentage became money and share counts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Position:
    """One holding in a strategy plan."""

    ticker: str
    sleeve: str
    name: Optional[str] = None
    sector: Optional[str] = None
    # Provenance from the selection ranking (None for instrument sleeves).
    candidate_role: Optional[str] = None
    risk_bucket: Optional[str] = None
    selection_rank: Optional[int] = None

    # Weighting inputs (what drove the size).
    score: Optional[float] = None            # allocation_adjusted_score
    fundamentals_score: Optional[float] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    volatility_pct: Optional[float] = None
    risk_penalty: Optional[float] = None

    # Weights as fractions of the TOTAL portfolio.
    target_weight: float = 0.0               # after caps, before share rounding
    realized_weight: float = 0.0             # after integer-share rounding

    # Money layer.
    price: Optional[float] = None
    target_value: float = 0.0
    shares: float = 0.0
    invested_value: float = 0.0
    commission: float = 0.0

    capped_by: Optional[str] = None          # "name" | "sector" | None
    warnings: List[str] = field(default_factory=list)

    def to_row(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "sleeve": self.sleeve,
            "sector": self.sector,
            "candidate_role": self.candidate_role,
            "risk_bucket": self.risk_bucket,
            "selection_rank": self.selection_rank,
            "target_weight_pct": round(100.0 * self.target_weight, 4),
            "realized_weight_pct": round(100.0 * self.realized_weight, 4),
            "price": self.price,
            "target_value": round(self.target_value, 2),
            "shares": self.shares,
            "invested_value": round(self.invested_value, 2),
            "commission": round(self.commission, 2),
            "score": self.score,
            "fundamentals_score": self.fundamentals_score,
            "sentiment_score": self.sentiment_score,
            "sentiment_confidence": self.sentiment_confidence,
            "volatility_pct": self.volatility_pct,
            "risk_penalty": self.risk_penalty,
            "capped_by": self.capped_by,
            "warnings": sorted(set(self.warnings)),
        }


@dataclass
class SleeveResult:
    """Target vs. achieved budget for one sleeve."""

    sleeve: str
    target_budget: float = 0.0       # from the strategy config
    allocated_weight: float = 0.0    # sum of position target weights
    overflow_weight: float = 0.0     # budget that could not be placed here
    position_count: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sleeve": self.sleeve,
            "target_budget_pct": round(100.0 * self.target_budget, 4),
            "allocated_weight_pct": round(100.0 * self.allocated_weight, 4),
            "overflow_weight_pct": round(100.0 * self.overflow_weight, 4),
            "position_count": self.position_count,
            "notes": self.notes,
        }


@dataclass
class StrategyPlan:
    """A complete allocation plan for one risk strategy."""

    strategy: str
    label: str
    description: str = ""
    positions: List[Position] = field(default_factory=list)
    sleeves: List[SleeveResult] = field(default_factory=list)

    investable_amount: float = 0.0
    invested_value: float = 0.0
    commission_total: float = 0.0
    cash_value: float = 0.0          # cash sleeve + unplaceable residual
    residual_cash: float = 0.0       # rounding leftovers only

    # Diversification diagnostics.
    sector_weights: Dict[str, float] = field(default_factory=dict)
    equity_weight: float = 0.0
    largest_position_weight: float = 0.0
    effective_positions: float = 0.0  # 1 / sum(w^2) over invested positions

    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "label": self.label,
            "description": self.description,
            "investable_amount": round(self.investable_amount, 2),
            "invested_value": round(self.invested_value, 2),
            "commission_total": round(self.commission_total, 2),
            "cash_value": round(self.cash_value, 2),
            "residual_cash": round(self.residual_cash, 2),
            "equity_weight_pct": round(100.0 * self.equity_weight, 4),
            "largest_position_weight_pct": round(100.0 * self.largest_position_weight, 4),
            "effective_positions": round(self.effective_positions, 2),
            "sector_weights_pct": {
                k: round(100.0 * v, 4) for k, v in sorted(self.sector_weights.items())
            },
            "sleeves": [s.to_dict() for s in self.sleeves],
            "positions": [p.to_row() for p in self.positions],
            "warnings": sorted(set(self.warnings)),
            "notes": self.notes,
        }


@dataclass
class AllocationResult:
    """All strategies produced by one allocation run."""

    run_id: str
    generated_at: str
    plans: Dict[str, StrategyPlan] = field(default_factory=dict)
    # Provenance of the consumed selection ranking.
    source_run_id: Optional[str] = None
    source_run_status: Optional[str] = None
    source_ranking_validity: Optional[str] = None
    source_path: Optional[str] = None
    candidates_considered: int = 0
    candidates_eligible: int = 0
    status: str = "VALID"            # VALID | PARTIAL | DEGRADED | INVALID
    status_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "module": "asset_selection.allocation",
            "status": self.status,
            "status_reasons": self.status_reasons,
            "source": {
                "path": self.source_path,
                "run_id": self.source_run_id,
                "run_status": self.source_run_status,
                "ranking_validity": self.source_ranking_validity,
                "candidates_considered": self.candidates_considered,
                "candidates_eligible": self.candidates_eligible,
            },
            "strategies": {k: v.to_dict() for k, v in self.plans.items()},
        }
