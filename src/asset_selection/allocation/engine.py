"""Allocation engine: selection ranking -> sleeve weights -> money.

Weighting within a selection-filled sleeve with budget ``B``:

    s_i   = max(allocation_adjusted_score_i, SCORE_FLOOR)      (score evidence)
    v_i   = volatility_pct_i (annualized fraction; sleeve median if missing)
    raw_i = s_i ** (1 - tilt)  *  v_i ** (-tilt)
    w_i   = B * raw_i / sum(raw)

``tilt`` is ``inverse_vol_tilt``: 0 gives pure score weighting, 1 gives pure
inverse-volatility (risk-parity-lite), and the geometric blend keeps both
components strictly positive. The score already embeds fundamentals, news
sentiment (confidence-adjusted) and risk penalties, so this does not re-score
anything — it only sizes what selection already judged.

Concentration is then enforced in two passes:

1. **Per-name cap**, inside each sleeve: a position above
   ``max_weight_per_name`` is clamped and the excess is redistributed
   proportionally to the sleeve's remaining headroom.
2. **Per-sector cap**, portfolio-wide across every equity position (this is
   the "several different business types" requirement): an over-cap sector is
   scaled down pro-rata and the freed weight is offered back to other sectors
   inside the same sleeve, preserving the strategy's sleeve budget.

Anything that still cannot be placed overflows to the strategy's
``overflow_sleeve`` (a broad instrument, so it is always placeable) and is
reported — never silently dropped.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .config import (
    BROAD_SECTOR,
    INSTRUMENT_SLEEVES,
    ROLE_TO_SLEEVE,
    SELECTION_FILLED_SLEEVES,
    SLEEVE_BOND_PROXY,
    SLEEVE_CASH,
    SLEEVE_INDEX_CORE,
    SLEEVE_SPECULATIVE,
    PortfolioConfig,
    StrategyConfig,
)
from .models import Position, SleeveResult, StrategyPlan

logger = logging.getLogger(__name__)

SCORE_FLOOR = 1.0            # keeps the score factor strictly positive
DEFAULT_VOLATILITY = 0.30    # only used when a sleeve has no vol data at all
_MAX_CAP_ITERATIONS = 50
_EPS = 1e-9

PriceLookup = Callable[[str], Optional[float]]


# ---------------------------------------------------------------------------
# Candidate preparation
# ---------------------------------------------------------------------------

def _num(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def eligible_candidates(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Names a strategy may hold: selection-eligible, with a usable price.

    The speculative sleeve is special: selection marks those names
    ``exclude_from_allocation_by_default``, so they are *not*
    ``eligible_for_allocation``. They are admitted only when the strategy
    opted in, and only into the speculative sleeve.
    """
    if df.empty:
        return df

    out = df.copy()
    if "candidate_role" not in out.columns:
        return out.iloc[0:0]

    eligible = out.get("eligible_for_allocation")
    eligible = (
        eligible.fillna(False).astype(bool)
        if eligible is not None
        else pd.Series(False, index=out.index)
    )
    is_speculative = out["candidate_role"].astype(str) == "speculative_research_only"

    keep = eligible | (is_speculative if cfg.allow_speculative else False)
    out = out[keep]

    # A name needs a price to become shares, and a sleeve to belong to.
    out = out[out["candidate_role"].astype(str).isin(ROLE_TO_SLEEVE)]
    if "last_close" in out.columns:
        out = out[pd.to_numeric(out["last_close"], errors="coerce").fillna(0) > 0]

    # Risk-bucket admission applies to the non-speculative sleeves; the
    # speculative sleeve is governed by its own hard budget cap instead.
    if "risk_bucket" in out.columns and cfg.allowed_risk_buckets:
        spec = out["candidate_role"].astype(str) == "speculative_research_only"
        allowed = out["risk_bucket"].astype(str).isin(cfg.allowed_risk_buckets)
        out = out[allowed | spec]
    return out


def _sleeve_members(df: pd.DataFrame, sleeve: str, cfg: StrategyConfig) -> pd.DataFrame:
    role = next((r for r, s in ROLE_TO_SLEEVE.items() if s == sleeve), None)
    if role is None or df.empty:
        return df.iloc[0:0]
    members = df[df["candidate_role"].astype(str) == role]
    sort_col = (
        "allocation_adjusted_score"
        if "allocation_adjusted_score" in members.columns
        else "final_score"
    )
    if sort_col in members.columns:
        members = members.sort_values(
            [sort_col, "ticker"], ascending=[False, True]
        )
    return members.head(cfg.max_names_per_sleeve)


# ---------------------------------------------------------------------------
# Within-sleeve weighting
# ---------------------------------------------------------------------------

def raw_weights(rows: Sequence[dict], tilt: float) -> List[float]:
    """Geometric blend of score evidence and inverse volatility (see module doc)."""
    if not rows:
        return []
    vols = [v for v in (_num(r.get("volatility_pct")) for r in rows) if v and v > 0]
    fallback_vol = (sorted(vols)[len(vols) // 2] if vols else DEFAULT_VOLATILITY)

    out: List[float] = []
    for r in rows:
        score = _num(r.get("allocation_adjusted_score"))
        if score is None:
            score = _num(r.get("final_score")) or SCORE_FLOOR
        score = max(score, SCORE_FLOOR)
        vol = _num(r.get("volatility_pct"))
        if vol is None or vol <= 0:
            vol = fallback_vol
        out.append((score ** (1.0 - tilt)) * (vol ** (-tilt)))
    total = sum(out)
    return [w / total for w in out] if total > _EPS else [1.0 / len(rows)] * len(rows)


def _apply_name_cap(
    weights: List[float], budget: float, max_name: float
) -> Tuple[List[float], float, List[bool]]:
    """Clamp positions at ``max_name``, redistributing excess inside the sleeve.

    Returns (weights, unplaceable_overflow, capped_flags).
    """
    n = len(weights)
    capped = [False] * n
    if n == 0:
        return [], budget, capped

    current = [w * budget for w in weights]
    for _ in range(_MAX_CAP_ITERATIONS):
        excess = 0.0
        for i, w in enumerate(current):
            if w > max_name + _EPS:
                excess += w - max_name
                current[i] = max_name
                capped[i] = True
        if excess <= _EPS:
            break
        headroom = [max(0.0, max_name - w) for w in current]
        total_headroom = sum(headroom)
        if total_headroom <= _EPS:
            return current, excess, capped          # sleeve is saturated
        share = min(excess, total_headroom)
        for i, h in enumerate(headroom):
            if h > 0:
                current[i] += share * (h / total_headroom)
        if excess - share > _EPS:
            return current, excess - share, capped
    return current, 0.0, capped


# ---------------------------------------------------------------------------
# Portfolio-wide sector cap
# ---------------------------------------------------------------------------

def _enforce_sector_cap(
    positions: List[Position], max_sector: float, max_name: float
) -> float:
    """Scale over-cap sectors down, re-offering freed weight inside each sleeve.

    Broad instruments (index / bonds / cash) are diversified by construction
    and exempt. Returns the weight that could not be re-placed.
    """
    equities = [p for p in positions if p.sleeve not in INSTRUMENT_SLEEVES]
    if not equities:
        return 0.0

    unplaceable = 0.0
    for _ in range(_MAX_CAP_ITERATIONS):
        by_sector: Dict[str, List[Position]] = {}
        for p in equities:
            by_sector.setdefault(p.sector or "UNKNOWN", []).append(p)

        over = {
            sec: sum(p.target_weight for p in ps) - max_sector
            for sec, ps in by_sector.items()
            if sum(p.target_weight for p in ps) > max_sector + _EPS
        }
        if not over:
            return unplaceable

        freed_by_sleeve: Dict[str, float] = {}
        for sec, excess in over.items():
            members = by_sector[sec]
            total = sum(p.target_weight for p in members)
            for p in members:
                cut = excess * (p.target_weight / total) if total > _EPS else 0.0
                p.target_weight -= cut
                p.capped_by = "sector"
                freed_by_sleeve[p.sleeve] = freed_by_sleeve.get(p.sleeve, 0.0) + cut

        # Re-offer freed weight to positions in the same sleeve whose sector
        # still has room and which are below the per-name cap.
        for sleeve, freed in freed_by_sleeve.items():
            remaining = freed
            sector_totals = {
                sec: sum(p.target_weight for p in ps) for sec, ps in by_sector.items()
            }
            eligible: List[Position] = []
            for p in equities:
                if p.sleeve != sleeve or p.sector in over:
                    continue
                sec = p.sector or "UNKNOWN"
                if sector_totals.get(sec, 0.0) >= max_sector - _EPS:
                    continue
                if p.target_weight >= max_name - _EPS:
                    continue
                eligible.append(p)
            if not eligible:
                unplaceable += remaining
                continue
            headroom = {
                id(p): min(
                    max_name - p.target_weight,
                    max_sector - sector_totals.get(p.sector or "UNKNOWN", 0.0),
                )
                for p in eligible
            }
            total_headroom = sum(max(0.0, h) for h in headroom.values())
            if total_headroom <= _EPS:
                unplaceable += remaining
                continue
            share = min(remaining, total_headroom)
            for p in eligible:
                h = max(0.0, headroom[id(p)])
                if h > 0:
                    p.target_weight += share * (h / total_headroom)
            if remaining - share > _EPS:
                unplaceable += remaining - share
    return unplaceable


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

def _instrument_position(
    sleeve: str, ticker: str, name: str, weight: float
) -> Position:
    return Position(
        ticker=ticker,
        sleeve=sleeve,
        name=name,
        sector=BROAD_SECTOR,
        target_weight=weight,
    )


def build_plan(
    df: pd.DataFrame,
    strategy: str,
    scfg: StrategyConfig,
    cfg: PortfolioConfig,
    *,
    price_lookup: Optional[PriceLookup] = None,
) -> StrategyPlan:
    """Build one strategy's plan from the selection ranking."""
    plan = StrategyPlan(
        strategy=strategy,
        label=scfg.label or strategy,
        description=scfg.description,
        investable_amount=cfg.capital.investable_amount,
    )
    universe = eligible_candidates(df, scfg)
    positions: List[Position] = []
    sleeve_results: Dict[str, SleeveResult] = {
        s: SleeveResult(sleeve=s, target_budget=float(scfg.sleeves.get(s, 0.0)))
        for s in scfg.sleeves
    }
    overflow_to_instrument = 0.0

    # --- selection-filled sleeves -----------------------------------------
    for sleeve in SELECTION_FILLED_SLEEVES:
        budget = float(scfg.sleeves.get(sleeve, 0.0))
        result = sleeve_results.setdefault(
            sleeve, SleeveResult(sleeve=sleeve, target_budget=budget)
        )
        if budget <= 0:
            continue
        if sleeve == SLEEVE_SPECULATIVE and not scfg.allow_speculative:
            result.overflow_weight = budget
            result.notes.append("speculative sleeve disabled; budget redirected")
            overflow_to_instrument += budget
            continue

        members = _sleeve_members(universe, sleeve, scfg)
        rows = members.to_dict("records")
        if not rows:
            result.overflow_weight = budget
            result.notes.append(
                "no eligible names for this sleeve; budget redirected to "
                f"{scfg.overflow_sleeve}"
            )
            plan.warnings.append(f"EMPTY_SLEEVE:{sleeve}")
            overflow_to_instrument += budget
            continue

        weights = raw_weights(rows, scfg.inverse_vol_tilt)
        sized, unplaceable, capped = _apply_name_cap(
            weights, budget, scfg.max_weight_per_name
        )
        if unplaceable > _EPS:
            result.notes.append(
                f"{unplaceable:.2%} of the budget exceeded the per-name cap with "
                f"only {len(rows)} eligible name(s); redirected"
            )
            plan.warnings.append(f"SLEEVE_SATURATED:{sleeve}")
            overflow_to_instrument += unplaceable

        for row, weight, was_capped in zip(rows, sized, capped):
            pos = Position(
                ticker=str(row.get("ticker")),
                sleeve=sleeve,
                name=row.get("company_name") or None,
                sector=(str(row.get("sector")) if row.get("sector") else "UNKNOWN"),
                candidate_role=row.get("candidate_role"),
                risk_bucket=row.get("risk_bucket"),
                selection_rank=int(row["rank"]) if _num(row.get("rank")) else None,
                score=_num(row.get("allocation_adjusted_score")),
                fundamentals_score=_num(row.get("fundamentals_score")),
                sentiment_score=_num(row.get("sentiment_score")),
                sentiment_confidence=_num(row.get("sentiment_confidence")),
                volatility_pct=_num(row.get("volatility_pct")),
                risk_penalty=_num(row.get("risk_penalty")),
                price=_num(row.get("last_close")),
                target_weight=weight,
                capped_by="name" if was_capped else None,
            )
            if sleeve == SLEEVE_SPECULATIVE:
                pos.warnings.append("SPECULATIVE_RESEARCH_ONLY")
            positions.append(pos)

    # --- portfolio-wide sector cap ----------------------------------------
    unplaced = _enforce_sector_cap(
        positions, scfg.max_weight_per_sector, scfg.max_weight_per_name
    )
    if unplaced > _EPS:
        overflow_to_instrument += unplaced
        plan.warnings.append("SECTOR_CAP_OVERFLOW")
        plan.notes.append(
            f"{unplaced:.2%} could not stay within the "
            f"{scfg.max_weight_per_sector:.0%} per-sector cap and was redirected "
            f"to {scfg.overflow_sleeve}"
        )

    # --- instrument sleeves (+ overflow) ----------------------------------
    instrument_meta = {
        SLEEVE_INDEX_CORE: (
            cfg.instruments.index_core_ticker, cfg.instruments.index_core_name
        ),
        SLEEVE_BOND_PROXY: (
            cfg.instruments.bond_proxy_ticker, cfg.instruments.bond_proxy_name
        ),
    }
    for sleeve in INSTRUMENT_SLEEVES:
        budget = float(scfg.sleeves.get(sleeve, 0.0))
        result = sleeve_results.setdefault(
            sleeve, SleeveResult(sleeve=sleeve, target_budget=budget)
        )
        weight = budget
        if sleeve == scfg.overflow_sleeve and overflow_to_instrument > 0:
            weight += overflow_to_instrument
            result.notes.append(
                f"absorbed {overflow_to_instrument:.2%} redirected from other sleeves"
            )
        if weight <= 0:
            continue
        if sleeve == SLEEVE_CASH:
            plan.cash_value = weight * cfg.capital.investable_amount
            result.allocated_weight = weight
            continue
        ticker, name = instrument_meta[sleeve]
        pos = _instrument_position(sleeve, ticker, name, weight)
        if price_lookup is not None:
            pos.price = price_lookup(ticker)
        if pos.price is None:
            pos.warnings.append("PRICE_UNAVAILABLE_NO_SHARE_COUNT")
            plan.warnings.append(f"PRICE_UNAVAILABLE:{ticker}")
        positions.append(pos)

    # --- bookkeeping -------------------------------------------------------
    for sleeve, result in sleeve_results.items():
        members = [p for p in positions if p.sleeve == sleeve]
        if sleeve != SLEEVE_CASH:
            result.allocated_weight = sum(p.target_weight for p in members)
        result.position_count = len(members)
    plan.sleeves = [sleeve_results[s] for s in sleeve_results]
    plan.positions = positions
    return plan
