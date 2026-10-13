"""Turn the evidence on each holding into SELL / TRIM / HOLD / ADD / REVIEW.

The engine is a short list of ordered, readable rules over scored evidence --
not a black box -- so every recommendation can be traced to the facts that
produced it. Two behavioural traps are excluded by construction:

* **A loss alone never triggers a sale**, and **a gain alone never triggers a
  sale**. What you paid is history; the decision is about the stock *now*.
  (Selling winners early and riding losers is the well-documented
  "disposition effect".) Cost only shapes the outlook text ("time to break
  even").
* **A forecast only counts if it earned it.** Forecast evidence enters the
  composite only at horizons where walk-forward validation found the model
  significantly better than a random walk.

The thesis is about the **business**: it is read from the fundamentals score
of a full-universe selection ranking, the only place that relative score is
meaningful. Price-driven exclusions (a recent drop, high volatility) already
appear as trend and risk evidence, so they never count as a broken thesis --
that would be selling because the price fell. Missing data gives UNKNOWN,
never BROKEN. Index and bond funds are STRATEGIC: held for their role in the
allocation and only ever rebalanced.

Rules, first match wins:

1. REVIEW  -- no usable price, or the position cannot be valued.
2. SELL    -- thesis BROKEN: fundamentals below ``fundamentals_broken``.
3. TRIM    -- weight above the target band (or, for single stocks without a
              target, above the concentration cap): trim back to target/cap.
4. ADD     -- STRATEGIC holding below its target band: rebalance up.
5. TRIM    -- thesis WEAKENED and the composite evidence is negative:
              reduce by ``trim_fraction_on_weakness``.
6. ADD     -- thesis INTACT, composite >= ``add_threshold``, below target
              (or below the cap), no earnings report inside the blackout.
7. HOLD    -- everything else. A large drawdown adds a REVIEW flag, not a sale.

Sizing is whole shares (unless fractional is enabled), commission-inclusive,
and an ADD is funded from new cash plus SELL/TRIM proceeds -- never assumed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional

from .config import DecisionConfig, MonitoringConfig
from .models import (
    ADD,
    HOLD,
    REVIEW,
    SELL,
    THESIS_BROKEN,
    THESIS_INTACT,
    THESIS_STRATEGIC,
    THESIS_UNKNOWN,
    THESIS_WEAKENED,
    TRIM,
    Evidence,
    PositionReport,
    Recommendation,
)
from .valuation import conversion

# Exclusion reasons that describe price behaviour or risk, not the business.
_PRICE_RISK_REASONS = {
    "negative_recent_return", "high_volatility", "weak_price_trend",
    "excessive_risk_penalty", "watchlist_only",
}


def thesis_status(r: PositionReport, d: DecisionConfig) -> tuple:
    """(status, explanation) for the investment thesis on this holding."""
    h, s = r.holding, r.selection
    if h.strategic or s.is_etf:
        role = h.sleeve or "fund"
        return THESIS_STRATEGIC, (f"{role.replace('_', ' ')} holding: kept for its role in the allocation, "
                                  "not judged on company fundamentals")
    if not h.in_selection_coverage:
        return THESIS_UNKNOWN, "outside the US-equity selection model (Mexican listing)"
    if s.source is None:
        return THESIS_UNKNOWN, "no selection results available to check the thesis"
    if not s.in_ranking:
        return THESIS_UNKNOWN, (
            "not in the latest full ranking (outside its shortlist); re-run trading-us-equities "
            "on the full universe to score it"
        )
    if s.data_quality_bucket == "incomplete":
        return THESIS_UNKNOWN, (
            f"fundamentals data incomplete ({s.missing_metric_count or 'many'} metrics missing); "
            "a weak score here would reflect gaps, not the business"
        )
    f = s.fundamentals_score
    if f is None:
        return THESIS_UNKNOWN, "no fundamentals score in the ranking"
    context = ""
    business = [x for x in s.exclusion_reasons if x not in _PRICE_RISK_REASONS]
    if s.candidate_role:
        context = f"; selection role {s.candidate_role}"
    if f < d.fundamentals_broken:
        return THESIS_BROKEN, (f"fundamentals score {f:.1f} is below the broken threshold "
                               f"{d.fundamentals_broken:.0f}{context}")
    if f < d.fundamentals_weak:
        return THESIS_WEAKENED, f"fundamentals score {f:.1f} is below {d.fundamentals_weak:.0f}{context}"
    if business:
        return THESIS_WEAKENED, (f"fundamentals {f:.1f}, but selection excludes it for "
                                 f"{', '.join(business)}{context}")
    return THESIS_INTACT, f"fundamentals {f:.1f} (>= {d.fundamentals_weak:.0f}){context}"


def gather_evidence(r: PositionReport, cfg: MonitoringConfig) -> List[Evidence]:
    d = cfg.decision
    ev: List[Evidence] = []
    status, why = thesis_status(r, d)
    ev.append(Evidence("thesis", {THESIS_INTACT: 1.0, THESIS_WEAKENED: -1.0,
                                  THESIS_BROKEN: -2.0}.get(status, 0.0), f"{status}: {why}"))
    risky = [x for x in r.selection.exclusion_reasons
             if x in _PRICE_RISK_REASONS and x != "watchlist_only"]
    if risky and status != THESIS_STRATEGIC:
        ev.append(Evidence("risk", -0.5, "selection flags it for allocation risk: " + ", ".join(risky)))

    t = r.trend
    if t.trend_state == "UP":
        ev.append(Evidence("trend", 1.0, f"uptrend: price {t.price_vs_sma_long_pct:+.1f}% vs "
                                         f"{cfg.trend.sma_long}-day average, golden cross"))
    elif t.trend_state == "DOWN":
        ev.append(Evidence("trend", -1.0, f"downtrend: price {t.price_vs_sma_long_pct:+.1f}% vs "
                                          f"{cfg.trend.sma_long}-day average, death cross"))
    elif t.trend_state == "MIXED":
        ev.append(Evidence("trend", 0.0, f"mixed trend: price {t.price_vs_sma_long_pct:+.1f}% vs "
                                         f"{cfg.trend.sma_long}-day average, {t.sma_cross} cross"))
    else:
        ev.append(Evidence("trend", 0.0, "not enough history for the long moving average"))

    if t.momentum_12_1_pct is not None:
        m = t.momentum_12_1_pct
        ev.append(Evidence("momentum", 0.5 if m > 0 else -0.5,
                           f"12-1 month momentum {m:+.1f}%"))

    s = r.selection
    if s.effective_sentiment_score is not None:
        conf = s.sentiment_confidence or 0.0
        sc = s.effective_sentiment_score
        if conf < d.min_sentiment_confidence:
            ev.append(Evidence("sentiment", 0.0, f"news sentiment {sc:.0f}/100 at low confidence "
                                                 f"({conf:.2f}); ignored"))
        elif sc >= d.sentiment_positive:
            ev.append(Evidence("sentiment", 0.5, f"positive news sentiment {sc:.0f}/100 (confidence {conf:.2f})"))
        elif sc <= d.sentiment_negative:
            ev.append(Evidence("sentiment", -0.5, f"negative news sentiment {sc:.0f}/100 (confidence {conf:.2f})"))
        else:
            ev.append(Evidence("sentiment", 0.0, f"neutral news sentiment {sc:.0f}/100"))

    e = r.events
    if e.last_surprise_pct is not None:
        sur = e.last_surprise_pct
        beats = f"; beat {e.beats} of last {e.reports_counted}" if e.reports_counted else ""
        ev.append(Evidence("earnings", 0.25 if sur > 0 else -0.5,
                           f"last EPS surprise {sur:+.1f}% on {e.last_report_date}{beats}"))

    if t.volatility_regime_ratio is not None and t.volatility_regime_ratio >= cfg.trend.volatility_regime_alert:
        ev.append(Evidence("risk", -0.5, f"volatility {t.volatility_ann_pct:.0f}% is "
                                         f"{t.volatility_regime_ratio:.1f}x its 1-year median"))

    fc = r.forecast
    if fc is not None and fc.status == "ok" and fc.horizons:
        usable = [h for h in fc.horizons if fc.informative_at(h.horizon_days)]
        if not fc.directional:
            ev.append(Evidence("forecast", 0.0, f"{fc.backend} forecast has no directional view by "
                                                "design; it sizes risk but never votes on direction"))
        elif usable:
            h = usable[len(usable) // 2]
            tilt = max(-0.5, min(0.5, (h.p_up - 0.5) * 2.0))
            sk = fc.skill[h.horizon_days]
            ev.append(Evidence("forecast", tilt,
                               f"validated {fc.backend} forecast: P(up in {h.horizon_days}d) "
                               f"{100 * h.p_up:.0f}% (skill {100 * sk.skill:+.1f}% vs random walk, "
                               f"false-discovery-adjusted q={sk.p_value_adjusted:.3f})"))
        else:
            ev.append(Evidence("forecast", 0.0, f"{fc.backend} forecast not validated as better than "
                                                "a random walk; excluded from the decision"))
    return ev


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    total_value: float                 # current holdings, reporting currency
    targets: Dict[str, float]          # holding key / ticker -> target weight
    reporting: str
    new_cash: float = 0.0              # money added from outside the portfolio
    cap: float = 1.0                   # effective per-name cap (see effective_cap)


def effective_cap(configured: float, band: float, n_positions: int) -> float:
    """The per-name cap actually applied when no target portfolio is given.

    With N holdings a cap below 1/N cannot be met by any allocation -- three
    stocks under a 15% cap would all be "too big". The cap is therefore never
    tighter than an equal-weight share plus the rebalance band: concentration
    is judged against what this portfolio could reasonably look like.
    """
    if n_positions <= 0:
        return configured
    return max(configured, (1.0 + band) / n_positions)


def decide_all(
    reports: List[PositionReport],
    cfg: MonitoringConfig,
    *,
    total_value: float,
    targets: Optional[Dict[str, float]] = None,
    new_cash: float = 0.0,
    today: Optional[date] = None,
) -> float:
    """Decide every position, then fund ADDs. Returns cash left after the trades."""
    today = today or date.today()
    valued = sum(1 for r in reports if r.valuation.market_value_reporting is not None)
    ctx = _Ctx(total_value=total_value, targets=targets or {},
               reporting=cfg.run.reporting_currency, new_cash=float(new_cash),
               cap=effective_cap(cfg.decision.max_weight_per_name, cfg.decision.rebalance_band, valued))
    for r in reports:
        r.recommendation = _decide_one(r, cfg, ctx, today)

    proceeds = sum(-r.recommendation.value_delta_reporting - r.recommendation.commission_reporting
                   for r in reports if r.recommendation.action in (SELL, TRIM))
    if cfg.decision.reinvest_proceeds:
        return _fund_adds(reports, cfg, ctx, ctx.new_cash + proceeds)
    return _fund_adds(reports, cfg, ctx, ctx.new_cash) + proceeds


def _decide_one(r: PositionReport, cfg: MonitoringConfig, ctx: _Ctx, today: date) -> Recommendation:
    d = cfg.decision
    v = r.valuation
    rec = Recommendation()
    rec.evidence = gather_evidence(r, cfg)
    rec.composite_score = sum(e.score for e in rec.evidence)
    rec.thesis_status, _ = thesis_status(r, d)
    rec.current_weight = v.weight
    rec.target_weight = _target_for(r, ctx)
    rec.outlook = outlook_text(r)
    rec.review_by = _review_by(r, cfg, today)
    rec.confidence = _confidence(r, rec)

    # 1. REVIEW: cannot value the position.
    if v.market_value_reporting is None or v.last_price is None:
        rec.action, rec.rule = REVIEW, "no usable price or FX rate: the position cannot be valued"
        return rec
    if v.price_age_days is not None and v.price_age_days > cfg.run.max_price_age_days:
        rec.blockers.append(f"STALE_PRICE: last price is {v.price_age_days} days old")

    price_rep = _price_reporting(r, ctx)
    weight = v.weight or 0.0

    # 2. SELL: thesis broken.
    if rec.thesis_status == THESIS_BROKEN:
        _size_sale(rec, r, r.holding.shares, price_rep, d, ctx)
        rec.action, rec.rule = SELL, "thesis broken: " + _thesis_line(rec)
        return rec

    # 3. TRIM: overweight versus target band or cap.
    target = rec.target_weight
    if target is not None and weight > target * (1.0 + d.rebalance_band):
        _trim_to(rec, r, target, price_rep, d, ctx,
                 rule=f"overweight: {100 * weight:.1f}% vs target {100 * target:.1f}% "
                      f"(band +{100 * d.rebalance_band:.0f}%)")
        if rec.action == TRIM:
            return rec
    elif target is None and weight > ctx.cap and rec.thesis_status != THESIS_STRATEGIC:
        cap_note = ("" if ctx.cap <= d.max_weight_per_name + 1e-12 else
                    f", widened from {100 * d.max_weight_per_name:.0f}% because "
                    f"{100 * d.max_weight_per_name:.0f}% is unreachable with this few holdings")
        _trim_to(rec, r, ctx.cap, price_rep, d, ctx,
                 rule=f"concentration: {100 * weight:.1f}% exceeds the "
                      f"{100 * ctx.cap:.1f}% per-name cap{cap_note}")
        if rec.action == TRIM:
            return rec

    # 4. ADD: a strategic holding below its target band is rebalanced up,
    #    whatever its recent trend -- that is what holding an index sleeve means.
    if rec.thesis_status == THESIS_STRATEGIC:
        if target is not None and weight < target * (1.0 - d.rebalance_band):
            rec.action = ADD
            rec.rule = (f"rebalance: {100 * weight:.1f}% vs target {100 * target:.1f}% "
                        f"(band -{100 * d.rebalance_band:.0f}%)")
            return rec
        rec.action = HOLD
        rec.rule = ("strategic holding within its target band" if target is not None
                    else "strategic holding; pass --target to rebalance it against the plan")
        rec.post_trade_weight = weight
        return rec

    # 5. TRIM: weakened thesis and negative evidence.
    if rec.thesis_status == THESIS_WEAKENED and rec.composite_score < 0:
        sell_shares = _round_shares(r.holding.shares * d.trim_fraction_on_weakness, d, down=True)
        if sell_shares > 0 and sell_shares * price_rep >= d.min_trade_value:
            _size_sale(rec, r, sell_shares, price_rep, d, ctx)
            rec.action = TRIM
            rec.rule = (f"thesis weakened and evidence negative (composite {rec.composite_score:+.2f}): "
                        f"reduce by {100 * d.trim_fraction_on_weakness:.0f}%")
            return rec

    # 6. ADD candidate (funded later). Like TRIM, it waits for the band: a
    #    position a hair under its target is not worth an order.
    ceiling = target if target is not None else ctx.cap
    hold_reason: Optional[str] = None
    if rec.thesis_status == THESIS_INTACT and rec.composite_score >= d.add_threshold:
        if weight >= ceiling * (1.0 - d.rebalance_band):
            hold_reason = (f"thesis intact and evidence positive (composite {rec.composite_score:+.2f}), "
                           f"but already within its {'target' if target is not None else 'cap'} band "
                           f"({100 * weight:.1f}% vs {100 * ceiling:.1f}%)")
        elif r.events.in_earnings_blackout:
            rec.blockers.append(
                f"EARNINGS_BLACKOUT: earnings on {r.events.next_earnings_date} "
                f"({r.events.days_to_earnings} days); ADD deferred until after the report"
            )
            hold_reason = "thesis intact and evidence positive, but the ADD waits for the earnings report"
        elif any(b.startswith("STALE_PRICE") for b in rec.blockers):
            rec.blockers.append("ADD deferred: price is stale")
            hold_reason = "thesis intact and evidence positive, but the price is stale"
        else:
            rec.action = ADD
            rec.rule = (f"thesis intact, composite {rec.composite_score:+.2f} >= {d.add_threshold:+.2f}, "
                        f"{100 * weight:.1f}% vs {'target' if target is not None else 'cap'} "
                        f"{100 * ceiling:.1f}%")
            return rec

    # 7. HOLD.
    rec.action = HOLD
    dd = v.drawdown_from_peak_since_purchase_pct
    if dd is not None and dd <= -abs(d.drawdown_review_pct):
        rec.blockers.append(f"DRAWDOWN_REVIEW: {dd:.1f}% from the peak since purchase -- "
                            "re-check the thesis; a loss alone is not a reason to sell")
    rec.rule = hold_reason or _hold_rule(rec, d)
    rec.post_trade_weight = weight
    return rec


def _fund_adds(reports: List[PositionReport], cfg: MonitoringConfig, ctx: _Ctx, cash: float) -> float:
    """Spread available cash over ADD candidates, strongest evidence first."""
    d = cfg.decision
    # Rebalancing strategic sleeves comes first: it restores the plan's risk
    # profile. Stock ADDs follow, strongest evidence first.
    adds = sorted((r for r in reports if r.recommendation.action == ADD),
                  key=lambda r: (r.recommendation.thesis_status != THESIS_STRATEGIC,
                                 -r.recommendation.composite_score))
    if not adds:
        return cash
    if cash < d.min_trade_value:
        for r in adds:
            r.recommendation.blockers.append(
                "UNFUNDED: no new cash or sale proceeds available; pass --cash to size this ADD"
            )
            r.recommendation.post_trade_weight = r.valuation.weight
        return cash
    # Sale proceeds are value already inside the portfolio, so only new cash
    # (less commissions) grows it.
    portfolio_after = ctx.total_value + ctx.new_cash
    for r in adds:
        rec = r.recommendation
        price_rep = _price_reporting(r, ctx)
        target = rec.target_weight if rec.target_weight is not None else ctx.cap
        want_value = max(0.0, target * portfolio_after - (r.valuation.market_value_reporting or 0.0))
        budget = min(want_value, cash)
        unit = price_rep * (1.0 + d.commission_pct)
        shares = _round_shares(budget / unit, d, down=True) if unit > 0 else 0.0
        value = shares * price_rep
        if shares <= 0 or value < d.min_trade_value:
            rec.blockers.append(
                f"BELOW_MIN_TRADE: {ctx.reporting} {budget:,.0f} available buys "
                f"{shares:g} share(s); minimum trade is {ctx.reporting} {d.min_trade_value:,.0f}"
            )
            rec.action, rec.rule = HOLD, rec.rule + " -- but too small to execute; holding"
            rec.post_trade_weight = r.valuation.weight
            continue
        commission = value * d.commission_pct
        rec.shares_delta = shares
        rec.value_delta_reporting = value
        rec.commission_reporting = commission
        rec.post_trade_weight = ((r.valuation.market_value_reporting or 0.0) + value) / portfolio_after
        cash -= value + commission
    return cash


# ---------------------------------------------------------------------------
# Sizing helpers
# ---------------------------------------------------------------------------

def _price_reporting(r: PositionReport, ctx: _Ctx) -> float:
    v = r.valuation
    conv = conversion(r.holding.listing_currency, ctx.reporting, v.fx_rate)
    return float(v.last_price) * float(conv or 0.0)


def _round_shares(x: float, d: DecisionConfig, *, down: bool) -> float:
    if d.allow_fractional_shares:
        return math.floor(x * 1e6) / 1e6 if down else x
    return float(math.floor(x + 1e-9)) if down else float(math.ceil(x - 1e-9))


def _size_sale(rec: Recommendation, r: PositionReport, shares: float, price_rep: float,
               d: DecisionConfig, ctx: _Ctx) -> None:
    shares = min(shares, r.holding.shares)
    value = shares * price_rep
    rec.shares_delta = -shares
    rec.value_delta_reporting = -value
    rec.commission_reporting = value * d.commission_pct
    remaining = (r.valuation.market_value_reporting or 0.0) - value
    rec.post_trade_weight = remaining / ctx.total_value if ctx.total_value > 0 else None


def _trim_to(rec: Recommendation, r: PositionReport, weight_target: float, price_rep: float,
             d: DecisionConfig, ctx: _Ctx, *, rule: str) -> None:
    excess_value = (r.valuation.market_value_reporting or 0.0) - weight_target * ctx.total_value
    shares = _round_shares(excess_value / price_rep, d, down=False) if price_rep > 0 else 0.0
    shares = min(shares, r.holding.shares)
    if shares <= 0 or shares * price_rep < d.min_trade_value:
        rec.blockers.append(f"BELOW_MIN_TRADE: trimming to {100 * weight_target:.1f}% is under the "
                            "minimum trade size")
        return
    _size_sale(rec, r, shares, price_rep, d, ctx)
    rec.action, rec.rule = TRIM, rule


def _target_for(r: PositionReport, ctx: _Ctx) -> Optional[float]:
    for key in (r.holding.key, r.holding.ticker):
        if key in ctx.targets:
            return ctx.targets[key]
    return None


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _thesis_line(rec: Recommendation) -> str:
    for e in rec.evidence:
        if e.factor == "thesis":
            return e.summary.split(": ", 1)[-1]
    return rec.thesis_status


def _hold_rule(rec: Recommendation, d: DecisionConfig) -> str:
    c = rec.composite_score
    if rec.thesis_status == THESIS_INTACT:
        return f"thesis intact; evidence not strong enough to add (composite {c:+.2f})"
    if rec.thesis_status == THESIS_WEAKENED:
        return f"thesis weakened but evidence not negative (composite {c:+.2f}); watch"
    return f"thesis cannot be checked; holding on price evidence (composite {c:+.2f})"


def outlook_text(r: PositionReport) -> Optional[str]:
    """One plain sentence on how likely, and how soon, the stock reaches a level."""
    fc = r.forecast
    if fc is None or fc.status != "ok" or not fc.targets:
        return None
    longest = max(h.horizon_days for h in fc.horizons)
    edges = sorted(h for h, s in fc.skill.items() if s.informative)
    if not fc.directional:
        qual = "volatility-based; no directional view"
    elif edges:
        qual = f"{fc.backend}, beat a random walk at {', '.join(f'{h}d' for h in edges)}"
    else:
        qual = f"{fc.backend}, no validated edge over a random walk"
    chosen = [t for t in fc.targets if t.label == "break-even"][:1]
    chosen += [t for t in fc.targets if t.label != "break-even"][:1]
    parts = []
    for t in chosen:
        p_end = t.p_above_at.get(longest)
        touch = (t.p_touch_by or {}).get(longest)
        bit = f"{t.label}: {100 * p_end:.0f}% chance to be above it in ~{_months(longest)}"
        if touch is not None:
            bit += f" ({100 * touch:.0f}% to touch it at some point)"
        if t.median_crossing_day is not None:
            bit += f"; median path reaches it in ~{_months(t.median_crossing_day)}"
        else:
            bit += "; median path does not reach it in that window"
        parts.append(bit)
    return f"[{qual}] " + "; ".join(parts)


def _months(trading_days: int) -> str:
    m = trading_days / 21.0
    return f"{m:.0f} month{'s' if round(m) != 1 else ''}" if m >= 1 else f"{trading_days} trading days"


def _review_by(r: PositionReport, cfg: MonitoringConfig, today: date) -> str:
    if r.events.next_earnings_date and r.events.days_to_earnings is not None \
            and r.events.days_to_earnings >= 0:
        nxt = date.fromisoformat(r.events.next_earnings_date)
        return (nxt + timedelta(days=2)).isoformat()
    return (today + timedelta(days=30)).isoformat()


def _confidence(r: PositionReport, rec: Recommendation) -> str:
    have = sum([
        r.valuation.last_price is not None,
        r.trend.trend_state not in ("INSUFFICIENT",),
        r.selection.in_ranking,
        r.events.last_surprise_pct is not None or r.events.next_earnings_date is not None,
    ])
    signs = [e.score for e in rec.evidence if e.score != 0]
    agree = (all(s > 0 for s in signs) or all(s < 0 for s in signs)) if signs else False
    if have == 4 and agree and len(signs) >= 3:
        return "high"
    if have >= 3:
        return "medium"
    return "low"
