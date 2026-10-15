"""Domain model for a portfolio-monitoring run.

A recommendation is only as trustworthy as its inputs, so every layer keeps
its provenance: which lot came from which file and row, which price from which
date, which forecast from which model -- and how well that model actually did
when tested against a random walk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# Actions, ordered from most to least defensive.
SELL = "SELL"
TRIM = "TRIM"
HOLD = "HOLD"
ADD = "ADD"
REVIEW = "REVIEW"
ACTIONS = (SELL, TRIM, HOLD, ADD, REVIEW)

THESIS_INTACT = "INTACT"
THESIS_WEAKENED = "WEAKENED"
THESIS_BROKEN = "BROKEN"
THESIS_UNKNOWN = "UNKNOWN"
# Index / bond funds: held for their role in the allocation, not judged on
# company fundamentals. Only rebalancing applies to them.
THESIS_STRATEGIC = "STRATEGIC"
STRATEGIC_SLEEVES = ("index_core", "bond_proxy", "cash")


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    # NaN/inf have no JSON representation; "unknown" is None.
    if x is None or not math.isfinite(float(x)):
        return None
    return round(float(x), nd)


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    """One purchase (or one aggregated statement line) as the user holds it."""

    ticker: str                       # normalized root, e.g. NVDA, BRK.B, WALMEX
    shares: float
    unit_cost: float                  # per share, in ``currency``
    currency: str                     # MXN | USD
    market: str                       # US | SIC | BMV
    purchase_date: Optional[date] = None
    broker: Optional[str] = None
    source: str = ""                  # "holdings.csv row 3", "statement.pdf p2 table1"
    raw_ticker: Optional[str] = None
    derived_fields: List[str] = field(default_factory=list)   # e.g. ["unit_cost"]

    @property
    def cost(self) -> float:
        return self.shares * self.unit_cost


@dataclass
class Holding:
    """All lots of one instrument in one market and currency."""

    ticker: str
    market: str
    currency: str                     # currency the position is carried in
    analysis_symbol: str              # Yahoo symbol whose prices are analysed
    listing_currency: str             # currency of analysis_symbol's prices
    lots: List[Lot] = field(default_factory=list)
    in_selection_coverage: bool = True
    name: Optional[str] = None
    sector: Optional[str] = None
    # Allocation sleeve when known (from the target portfolio or the allocation
    # instruments), e.g. index_core / bond_proxy / equity_core.
    sleeve: Optional[str] = None
    is_fund: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def strategic(self) -> bool:
        return self.is_fund or (self.sleeve in STRATEGIC_SLEEVES)

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.market}|{self.currency}"

    @property
    def shares(self) -> float:
        return float(sum(l.shares for l in self.lots))

    @property
    def cost_basis(self) -> float:
        return float(sum(l.cost for l in self.lots))

    @property
    def avg_cost(self) -> Optional[float]:
        s = self.shares
        return self.cost_basis / s if s > 0 else None

    @property
    def purchase_dates(self) -> List[date]:
        return sorted(l.purchase_date for l in self.lots if l.purchase_date is not None)

    @property
    def first_purchase_date(self) -> Optional[date]:
        d = self.purchase_dates
        return d[0] if d else None

    @property
    def last_purchase_date(self) -> Optional[date]:
        d = self.purchase_dates
        return d[-1] if d else None

    @property
    def dates_complete(self) -> bool:
        return bool(self.lots) and all(l.purchase_date is not None for l in self.lots)


# ---------------------------------------------------------------------------
# Per-holding analysis layers
# ---------------------------------------------------------------------------

@dataclass
class Valuation:
    last_price: Optional[float] = None           # listing currency
    last_price_date: Optional[str] = None
    price_age_days: Optional[int] = None
    price_source: Optional[str] = None           # provider | user_override
    fx_rate: Optional[float] = None              # USDMXN used for conversion
    market_value: Optional[float] = None         # position currency
    unrealized_pl: Optional[float] = None
    unrealized_pl_pct: Optional[float] = None
    market_value_reporting: Optional[float] = None
    cost_basis_reporting: Optional[float] = None
    unrealized_pl_reporting: Optional[float] = None
    holding_days: Optional[int] = None
    # Split of an MXN-carried US stock's return into stock and currency parts.
    stock_return_pct: Optional[float] = None
    fx_return_pct: Optional[float] = None
    max_gain_since_purchase_pct: Optional[float] = None
    drawdown_from_peak_since_purchase_pct: Optional[float] = None
    weight: Optional[float] = None               # of the portfolio, reporting currency

    def to_dict(self) -> Dict[str, Any]:
        return {k: (_r(v) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class TrendSignals:
    history_days: int = 0
    sma_short: Optional[float] = None
    sma_long: Optional[float] = None
    price_vs_sma_long_pct: Optional[float] = None
    sma_cross: Optional[str] = None              # golden | death
    trend_state: str = "INSUFFICIENT"            # UP | DOWN | MIXED | INSUFFICIENT
    returns_pct: Dict[str, Optional[float]] = field(default_factory=dict)
    momentum_12_1_pct: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    pct_from_52w_high: Optional[float] = None
    volatility_ann_pct: Optional[float] = None
    volatility_regime_ratio: Optional[float] = None
    max_drawdown_1y_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {k: (_r(v) if isinstance(v, float) else v) for k, v in self.__dict__.items()}
        d["returns_pct"] = {k: _r(v) for k, v in self.returns_pct.items()}
        return d


@dataclass
class EventSignals:
    next_earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    in_earnings_blackout: bool = False
    last_report_date: Optional[str] = None
    last_surprise_pct: Optional[float] = None
    beats: Optional[int] = None
    reports_counted: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: (_r(v) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class SelectionSignals:
    """What the selection pipeline currently thinks of this name."""

    source: Optional[str] = None                 # full_ranking | refreshed | None
    run_id: Optional[str] = None
    in_ranking: bool = False
    company_name: Optional[str] = None
    sector: Optional[str] = None
    selection_rank: Optional[int] = None
    fundamentals_score: Optional[float] = None
    final_score: Optional[float] = None
    effective_sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    article_count: Optional[int] = None
    risk_penalty: Optional[float] = None
    candidate_role: Optional[str] = None
    eligible_for_allocation: Optional[bool] = None
    flags: List[str] = field(default_factory=list)
    exclusion_reasons: List[str] = field(default_factory=list)
    data_quality_bucket: Optional[str] = None
    missing_metric_count: Optional[int] = None
    is_etf: Optional[bool] = None
    # Where the sentiment fields came from when a refresh supplied them.
    sentiment_source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: (_r(v) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

@dataclass
class HorizonForecast:
    horizon_days: int
    quantiles: Dict[float, float]                # level -> price (listing currency)
    p_up: float                                  # P(price at horizon > today)

    @property
    def median(self) -> float:
        return self.quantiles[0.5]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "quantiles": {str(k): _r(v) for k, v in self.quantiles.items()},
            "median": _r(self.median),
            "p_up": _r(self.p_up),
        }


@dataclass
class TargetOutlook:
    """How likely, and how soon, the price reaches a level."""

    label: str                                   # "+10%", "break-even"
    target_price: float                          # listing currency
    p_above_at: Dict[int, float]                 # horizon -> P(price above target then)
    # First trading day at which the forecast puts >= 50% on being above the
    # target. None = not within the longest horizon.
    median_crossing_day: Optional[int] = None
    # First-passage probability (touch at any point), only for path backends.
    p_touch_by: Optional[Dict[int, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "target_price": _r(self.target_price),
            "p_above_at": {str(k): _r(v) for k, v in self.p_above_at.items()},
            "median_crossing_day": self.median_crossing_day,
            "p_touch_by": None if self.p_touch_by is None
            else {str(k): _r(v) for k, v in self.p_touch_by.items()},
        }


@dataclass
class SkillReport:
    """Out-of-sample accuracy against a random-walk baseline at one horizon."""

    horizon_days: int
    n_origins: int
    loss_model: float                            # mean quantile (pinball) loss
    loss_baseline: float
    skill: float                                 # 1 - loss_model / loss_baseline
    dm_stat: Optional[float]
    p_value: Optional[float]                     # one-sided: model better
    hit_rate: Optional[float]                    # direction of the median
    hit_rate_ci: Optional[List[float]]
    coverage_80: Optional[float]                 # share of outcomes in [q10, q90]
    informative: bool
    # Holm-adjusted across this ticker's horizons (several tests, one claim).
    p_value_adjusted: Optional[float] = None
    # n_origins * step / horizon: roughly how many non-overlapping windows.
    effective_windows: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "n_origins": self.n_origins,
            "loss_model": _r(self.loss_model, 6),
            "loss_baseline": _r(self.loss_baseline, 6),
            "skill": _r(self.skill),
            "dm_stat": _r(self.dm_stat),
            "p_value": _r(self.p_value),
            "hit_rate": _r(self.hit_rate),
            "hit_rate_ci": None if self.hit_rate_ci is None else [_r(x) for x in self.hit_rate_ci],
            "coverage_80": _r(self.coverage_80),
            "p_value_adjusted": _r(self.p_value_adjusted),
            "effective_windows": _r(self.effective_windows, 2),
            "informative": self.informative,
        }


@dataclass
class ForecastResult:
    backend: str
    model_id: Optional[str] = None
    # False for models built with no view on direction (the demeaned
    # bootstrap): their P(up) is noise and never enters a decision.
    directional: bool = False
    as_of: Optional[str] = None                  # last date of the context
    last_price: Optional[float] = None
    horizons: List[HorizonForecast] = field(default_factory=list)
    targets: List[TargetOutlook] = field(default_factory=list)
    skill: Dict[int, SkillReport] = field(default_factory=dict)
    status: str = "ok"                           # ok | insufficient_history | error | disabled
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def informative_at(self, horizon: int) -> bool:
        s = self.skill.get(horizon)
        return bool(s and s.informative)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "model_id": self.model_id,
            "directional": self.directional,
            "as_of": self.as_of,
            "last_price": _r(self.last_price),
            "status": self.status,
            "error": self.error,
            "horizons": [h.to_dict() for h in self.horizons],
            "targets": [t.to_dict() for t in self.targets],
            "skill": {str(k): v.to_dict() for k, v in self.skill.items()},
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    factor: str                                  # thesis | trend | momentum | sentiment | ...
    score: float                                 # signed contribution to the composite
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {"factor": self.factor, "score": round(self.score, 3), "summary": self.summary}


@dataclass
class Recommendation:
    action: str = HOLD
    shares_delta: float = 0.0                    # + buy, - sell
    value_delta_reporting: float = 0.0           # money moved, reporting currency
    commission_reporting: float = 0.0
    current_weight: Optional[float] = None
    target_weight: Optional[float] = None
    post_trade_weight: Optional[float] = None
    composite_score: float = 0.0
    confidence: str = "low"                      # high | medium | low
    thesis_status: str = THESIS_UNKNOWN
    rule: str = ""                               # the rule that decided the action
    evidence: List[Evidence] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    outlook: Optional[str] = None
    review_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "shares_delta": _r(self.shares_delta, 6),
            "value_delta_reporting": round(self.value_delta_reporting, 2),
            "commission_reporting": round(self.commission_reporting, 2),
            "current_weight_pct": None if self.current_weight is None else round(100 * self.current_weight, 4),
            "target_weight_pct": None if self.target_weight is None else round(100 * self.target_weight, 4),
            "post_trade_weight_pct": None if self.post_trade_weight is None
            else round(100 * self.post_trade_weight, 4),
            "composite_score": round(self.composite_score, 3),
            "confidence": self.confidence,
            "thesis_status": self.thesis_status,
            "rule": self.rule,
            "evidence": [e.to_dict() for e in self.evidence],
            "blockers": self.blockers,
            "outlook": self.outlook,
            "review_by": self.review_by,
        }


@dataclass
class PositionReport:
    holding: Holding
    valuation: Valuation = field(default_factory=Valuation)
    trend: TrendSignals = field(default_factory=TrendSignals)
    events: EventSignals = field(default_factory=EventSignals)
    selection: SelectionSignals = field(default_factory=SelectionSignals)
    forecast: Optional[ForecastResult] = None
    recommendation: Recommendation = field(default_factory=Recommendation)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        h = self.holding
        return {
            "ticker": h.ticker,
            "market": h.market,
            "currency": h.currency,
            "analysis_symbol": h.analysis_symbol,
            "listing_currency": h.listing_currency,
            "name": h.name,
            "sector": h.sector,
            "shares": _r(h.shares, 6),
            "avg_cost": _r(h.avg_cost),
            "cost_basis": _r(h.cost_basis, 2),
            "first_purchase_date": h.first_purchase_date.isoformat() if h.first_purchase_date else None,
            "last_purchase_date": h.last_purchase_date.isoformat() if h.last_purchase_date else None,
            "lots": [
                {
                    "shares": l.shares, "unit_cost": l.unit_cost, "currency": l.currency,
                    "purchase_date": l.purchase_date.isoformat() if l.purchase_date else None,
                    "source": l.source, "derived_fields": l.derived_fields,
                }
                for l in h.lots
            ],
            "in_selection_coverage": h.in_selection_coverage,
            "valuation": self.valuation.to_dict(),
            "trend": self.trend.to_dict(),
            "events": self.events.to_dict(),
            "selection": self.selection.to_dict(),
            "forecast": None if self.forecast is None else self.forecast.to_dict(),
            "recommendation": self.recommendation.to_dict(),
            "warnings": sorted(set(self.warnings + h.warnings)),
        }


@dataclass
class MonitorResult:
    run_id: str
    generated_at: str
    reporting_currency: str
    positions: List[PositionReport] = field(default_factory=list)
    fx_rate: Optional[float] = None
    fx_as_of: Optional[str] = None
    total_value_reporting: float = 0.0
    total_cost_reporting: float = 0.0
    total_pl_reporting: float = 0.0
    new_cash_reporting: float = 0.0
    cash_after_trades_reporting: float = 0.0
    holdings_source: Optional[str] = None
    extraction: Optional[Dict[str, Any]] = None
    selection_source: Optional[Dict[str, Any]] = None
    target_source: Optional[str] = None
    forecast_backend: Optional[str] = None
    forecast_model: Optional[str] = None
    status: str = "VALID"                        # VALID | PARTIAL | DEGRADED | INVALID
    status_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "module": "asset_selection.monitoring",
            "status": self.status,
            "status_reasons": self.status_reasons,
            "reporting_currency": self.reporting_currency,
            "fx_usdmxn": _r(self.fx_rate),
            "fx_as_of": self.fx_as_of,
            "totals": {
                "market_value": round(self.total_value_reporting, 2),
                "cost_basis": round(self.total_cost_reporting, 2),
                "unrealized_pl": round(self.total_pl_reporting, 2),
                "new_cash": round(self.new_cash_reporting, 2),
                "cash_after_trades": round(self.cash_after_trades_reporting, 2),
            },
            "sources": {
                "holdings": self.holdings_source,
                "extraction": self.extraction,
                "selection": self.selection_source,
                "target_portfolio": self.target_source,
                "forecast_backend": self.forecast_backend,
                "forecast_model": self.forecast_model,
            },
            "positions": [p.to_dict() for p in self.positions],
        }
