"""Money layer: target weights -> dollars, whole shares, commission, cash.

Share count is computed against the **commission-inclusive** unit cost so a
plan never overspends its budget:

    shares = floor( target_value / (price * (1 + commission_pct)) )

With ``allow_fractional_shares`` the floor is skipped. A position whose target
value falls below ``min_position_value`` is reported as un-investable at this
capital level (its money stays in cash) rather than emitting a 0-share row.
"""
from __future__ import annotations

import math
from typing import Dict, List

from .config import INSTRUMENT_SLEEVES, SLEEVE_CASH, PortfolioConfig
from .models import Position, StrategyPlan

_EPS = 1e-9


def _size_position(pos: Position, cfg: PortfolioConfig) -> None:
    cap = cfg.capital
    pos.target_value = pos.target_weight * cap.investable_amount

    if pos.price is None or pos.price <= 0:
        # Honest outcome: a dollar target with no executable share count.
        pos.shares = 0.0
        pos.invested_value = 0.0
        pos.commission = 0.0
        return

    if pos.target_value < cap.min_position_value - _EPS:
        pos.shares = 0.0
        pos.invested_value = 0.0
        pos.commission = 0.0
        pos.warnings.append("BELOW_MIN_POSITION_VALUE")
        return

    unit_cost = pos.price * (1.0 + cap.commission_per_trade_pct)
    raw_shares = pos.target_value / unit_cost
    shares = raw_shares if cap.allow_fractional_shares else math.floor(raw_shares)
    if shares <= 0:
        pos.shares = 0.0
        pos.invested_value = 0.0
        pos.commission = 0.0
        pos.warnings.append("PRICE_EXCEEDS_TARGET_VALUE")
        return

    pos.shares = round(shares, 6) if cap.allow_fractional_shares else int(shares)
    pos.invested_value = pos.shares * pos.price
    pos.commission = pos.invested_value * cap.commission_per_trade_pct


def finalize_plan(plan: StrategyPlan, cfg: PortfolioConfig) -> StrategyPlan:
    """Convert a weighted plan into money, shares and diversification stats."""
    investable = cfg.capital.investable_amount

    for pos in plan.positions:
        _size_position(pos, cfg)
        pos.realized_weight = (
            pos.invested_value / investable if investable > 0 else 0.0
        )

    plan.invested_value = sum(p.invested_value for p in plan.positions)
    plan.commission_total = sum(p.commission for p in plan.positions)

    # Cash = the cash sleeve's target plus whatever rounding / un-investable
    # targets left behind. By construction this is never negative, because
    # share counts are floored against the commission-inclusive unit cost.
    cash_target = plan.cash_value
    spent = plan.invested_value + plan.commission_total
    plan.residual_cash = max(0.0, investable - cash_target - spent)
    plan.cash_value = cash_target + plan.residual_cash

    _add_diagnostics(plan, investable)
    return plan


def _add_diagnostics(plan: StrategyPlan, investable: float) -> None:
    invested_positions = [p for p in plan.positions if p.invested_value > 0]

    sector_weights: Dict[str, float] = {}
    for p in invested_positions:
        key = p.sector or "UNKNOWN"
        sector_weights[key] = sector_weights.get(key, 0.0) + p.realized_weight
    plan.sector_weights = sector_weights

    equities = [p for p in invested_positions if p.sleeve not in INSTRUMENT_SLEEVES]
    plan.equity_weight = sum(p.realized_weight for p in equities)
    plan.largest_position_weight = max(
        (p.realized_weight for p in invested_positions), default=0.0
    )
    # Effective number of positions = 1 / HHI over weights normalized to the
    # invested book. Normalizing matters: without it, a portfolio whose
    # instrument sleeves could not be priced would report an absurdly high
    # diversification just because every weight is small.
    invested_total = sum(p.realized_weight for p in invested_positions)
    if invested_total > _EPS:
        hhi = sum((p.realized_weight / invested_total) ** 2 for p in invested_positions)
        plan.effective_positions = (1.0 / hhi) if hhi > _EPS else 0.0
    else:
        plan.effective_positions = 0.0

    dropped = [p for p in plan.positions if p.invested_value <= 0 and p.target_value > 0]
    if dropped:
        plan.warnings.append("POSITIONS_NOT_INVESTABLE")
        plan.notes.append(
            f"{len(dropped)} position(s) could not be bought at this capital "
            f"level or without a price: {', '.join(sorted(p.ticker for p in dropped))}. "
            "Their money stays in cash."
        )
    if plan.cash_value > 0 and investable > 0:
        cash_pct = plan.cash_value / investable
        cash_budget = next(
            (s.target_budget for s in plan.sleeves if s.sleeve == SLEEVE_CASH), 0.0
        )
        if cash_pct > cash_budget + 0.05:
            plan.notes.append(
                f"Cash ended at {cash_pct:.1%} versus a {cash_budget:.1%} budget "
                "because of share rounding and un-investable positions."
            )


def order_rows(plan: StrategyPlan) -> List[Dict[str, object]]:
    """Flat order ticket: what to buy, how many, for how much."""
    rows: List[Dict[str, object]] = []
    for p in sorted(
        plan.positions, key=lambda x: (-x.realized_weight, x.sleeve, x.ticker)
    ):
        if p.shares <= 0:
            continue
        rows.append({
            "ticker": p.ticker,
            "sleeve": p.sleeve,
            "action": "BUY",
            "shares": p.shares,
            "price": round(p.price, 4) if p.price else None,
            "gross_value": round(p.invested_value, 2),
            "commission": round(p.commission, 2),
            "total_cost": round(p.invested_value + p.commission, 2),
            "weight_pct": round(100.0 * p.realized_weight, 3),
        })
    return rows
