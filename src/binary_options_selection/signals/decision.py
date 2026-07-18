"""CALL / PUT / NO_TRADE decision rules.

``NO_TRADE`` is a first-class result, not an error: whenever the evidence is
too close, too weak, contradictory, stale, thin, or the session is unsuitable,
the honest output is "do not trade this horizon now", with a machine-readable
reason.

``signal_validity`` describes the trustworthiness of the *data* behind the
row: ``valid`` (all gates passed cleanly), ``degraded`` (gates passed but
with warnings — reduced feature coverage, volatility regime flags, elevated
missing candles), ``invalid`` (a hard data gate failed; the signal is forced
to NO_TRADE). A NO_TRADE on *valid* data is a legitimate answer, not a
failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ..config import (
    HORIZON_MAX_MINUTES,
    HORIZON_MIN_MINUTES,
    SUPPORTED_INTERVALS,
    BinaryOptionsConfig,
)
from ..data_providers.base import Candle
from ..market_session import (
    MARKET_245,
    MARKET_247,
    MARKET_OPEN,
    MARKET_UNKNOWN,
    SessionState,
    classify_session_for_asset,
)
from ..scoring.directional import DirectionalScores
from ..validation.candles import SeriesQuality

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..catalog.context import ContractContext

SIGNAL_CALL = "CALL"
SIGNAL_PUT = "PUT"
SIGNAL_NO_TRADE = "NO_TRADE"

# A binary-options recommendation is the PURCHASE of an upward or downward
# contract -- never buying/selling the underlying itself, and never "SELL"
# (closing an open contract is platform-specific and is not modeled).
ACTION_BUY_CALL = "BUY_CALL"
ACTION_BUY_PUT = "BUY_PUT"
ACTION_NO_TRADE = "NO_TRADE"

_ACTION_FOR_SIGNAL = {
    SIGNAL_CALL: ACTION_BUY_CALL,
    SIGNAL_PUT: ACTION_BUY_PUT,
    SIGNAL_NO_TRADE: ACTION_NO_TRADE,
}


def recommended_action_for(signal: str) -> str:
    """Map a signal to its canonical recommended contract action."""
    return _ACTION_FOR_SIGNAL.get(signal, ACTION_NO_TRADE)

VALIDITY_VALID = "valid"
VALIDITY_DEGRADED = "degraded"
VALIDITY_INVALID = "invalid"

STRENGTH_STRONG = "strong"
STRENGTH_MODERATE = "moderate"
STRENGTH_WEAK = "weak"
STRENGTH_NONE = "none"

# --- machine-readable NO_TRADE reasons (canonical taxonomy) -----------------
# Evidence gates
NO_TRADE_SCORES_TOO_CLOSE = "WEAK_DIRECTIONAL_MARGIN"
NO_TRADE_LOW_CONFIDENCE = "LOW_CONFIDENCE"
NO_TRADE_CONTRADICTORY = "CONTRADICTORY_EVIDENCE"
NO_TRADE_NO_FEATURES = "INSUFFICIENT_FEATURE_COVERAGE"
# Market-data gates
NO_TRADE_STALE_DATA = "STALE_MARKET_DATA"
NO_TRADE_DATA_NOT_REAL_TIME = "DATA_NOT_REAL_TIME"
NO_TRADE_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
NO_TRADE_LOW_DATA_QUALITY = "LOW_DATA_QUALITY"
NO_TRADE_MISSING_CANDLES = "MISSING_CANDLES"
NO_TRADE_NO_COMPLETED_CANDLE = "INCOMPLETE_LATEST_CANDLE"
NO_TRADE_LOW_LIQUIDITY = "LOW_LIQUIDITY"
NO_TRADE_PROVIDER_COVERAGE = "PROVIDER_UNAVAILABLE"
NO_TRADE_ABNORMAL_DATA = "INVALID_OHLC"
NO_TRADE_ABNORMAL_MARKET = "ABNORMAL_MARKET_CONDITION"
NO_TRADE_SUB_MINIMUM_PRICE = "SUB_MINIMUM_PRICE"
NO_TRADE_DUPLICATE_CANDLES = "DUPLICATE_CANDLES"
# Session / timing gates
NO_TRADE_MARKET_CLOSED = "MARKET_CLOSED"
NO_TRADE_SESSION_NOT_SUPPORTED = "SESSION_NOT_SUPPORTED"
NO_TRADE_EXPIRY_BEYOND_SESSION = "EXPIRY_BEYOND_SESSION"
NO_TRADE_UNSUPPORTED_HORIZON = "UNSUPPORTED_HORIZON"
NO_TRADE_TIME_TO_EXPIRY_TOO_SHORT = "TIME_TO_EXPIRY_TOO_SHORT"
# Contract / catalog gates
NO_TRADE_INSTRUMENT_NOT_AVAILABLE = "INSTRUMENT_NOT_AVAILABLE"
NO_TRADE_AVAILABILITY_UNKNOWN = "INSTRUMENT_AVAILABILITY_UNKNOWN"
NO_TRADE_CATALOG_STALE = "CATALOG_STALE"
NO_TRADE_EXPIRATION_NOT_AVAILABLE = "EXPIRATION_NOT_AVAILABLE"
NO_TRADE_CONTRACT_CLOSED = "CONTRACT_CLOSED"
NO_TRADE_OTC_EXCLUDED = "OTC_EXCLUDED"
NO_TRADE_PAYOUT_UNKNOWN = "PAYOUT_UNKNOWN"
NO_TRADE_PAYOUT_STALE = "PAYOUT_STALE"
NO_TRADE_PAYOUT_BELOW_MINIMUM = "PAYOUT_BELOW_MINIMUM"
NO_TRADE_PLATFORM_QUOTE_STALE = "PLATFORM_QUOTE_STALE"
NO_TRADE_QUOTE_MISMATCH = "QUOTE_MISMATCH"

# Warning flags used by the contract layer
WARN_OTC_CONTRACT = "OTC_CONTRACT"
WARN_RESEARCH_MODE = "RESEARCH_MODE_ROW"
WARN_NO_CATALOG = "NO_VERIFIED_CATALOG"


@dataclass
class LiquidityAssessment:
    ok: bool = False
    avg_volume: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    zero_volume_ratio: float = 1.0
    reasons: List[str] = field(default_factory=list)


def assess_liquidity(candles: List[Candle], cfg: BinaryOptionsConfig) -> LiquidityAssessment:
    """Per-bar liquidity screen over the cleaned candle window."""
    out = LiquidityAssessment()
    if not candles:
        out.reasons.append("no candles")
        return out
    n = len(candles)
    out.avg_volume = sum(c.volume for c in candles) / n
    out.avg_dollar_volume = sum(c.volume * c.close for c in candles) / n
    out.zero_volume_ratio = sum(1 for c in candles if c.volume == 0) / n
    liq = cfg.liquidity
    if out.avg_volume < liq.min_avg_volume:
        out.reasons.append(
            f"avg volume {out.avg_volume:,.0f} < required {liq.min_avg_volume:,.0f}"
        )
    if out.avg_dollar_volume < liq.min_avg_dollar_volume:
        out.reasons.append(
            f"avg dollar volume {out.avg_dollar_volume:,.0f} < required "
            f"{liq.min_avg_dollar_volume:,.0f}"
        )
    if out.zero_volume_ratio > liq.max_zero_volume_ratio:
        out.reasons.append(
            f"zero-volume ratio {out.zero_volume_ratio:.0%} > allowed "
            f"{liq.max_zero_volume_ratio:.0%}"
        )
    out.ok = not out.reasons
    return out


@dataclass
class SignalDecision:
    signal: str = SIGNAL_NO_TRADE
    signal_strength: str = STRENGTH_NONE
    signal_validity: str = VALIDITY_INVALID
    no_trade_reason: Optional[str] = None
    warning_flags: List[str] = field(default_factory=list)

    @property
    def recommended_action(self) -> str:
        return recommended_action_for(self.signal)


def decide_signal(
    scores: DirectionalScores,
    quality: SeriesQuality,
    session: SessionState,
    liquidity: LiquidityAssessment,
    *,
    expiry_utc: datetime,
    cfg: BinaryOptionsConfig,
    contract: Optional["ContractContext"] = None,
    market_delay_seconds: float = 0.0,
    abnormal_market: bool = False,
    asset_type: Optional[str] = None,
) -> SignalDecision:
    """Decide one (instrument, horizon) row.

    ``contract`` carries what the verified catalog says about this row. When
    it is ``None`` the caller is doing library-level/research analysis and the
    contract gates are skipped -- the pipeline itself ALWAYS passes a context
    (an availability-unknown one when no catalog is configured), so a real run
    can never silently treat market data as a tradable contract.
    """
    d = SignalDecision()
    d.warning_flags = list(quality.flags)
    rules = cfg.signal_rules

    # ------------------------------------------------------------------
    # Hard data / context gates -> NO_TRADE with validity "invalid".
    # Ordered from most to least fundamental so the reported reason is the
    # root cause, not a symptom.
    # ------------------------------------------------------------------
    h = scores.horizon_minutes
    interval_minutes = SUPPORTED_INTERVALS.get(cfg.candles.interval, 0)
    if (
        h < HORIZON_MIN_MINUTES or h > HORIZON_MAX_MINUTES
        or interval_minutes <= 0 or h % interval_minutes != 0
    ):
        return _no_trade(d, NO_TRADE_UNSUPPORTED_HORIZON)

    # ------------------------------------------------------------------
    # Contract / catalog gates (most fundamental after horizon support):
    # a signal on a contract that cannot be traded is meaningless.
    # ------------------------------------------------------------------
    if contract is not None:
        gate = _contract_gate(d, contract)
        if gate is not None:
            return gate
    if quality.error_type and not quality.candles:
        return _no_trade(d, NO_TRADE_PROVIDER_COVERAGE)
    if not quality.candles:
        return _no_trade(d, NO_TRADE_NO_COMPLETED_CANDLE)
    if quality.insufficient_history:
        return _no_trade(d, NO_TRADE_INSUFFICIENT_HISTORY)
    if quality.stale:
        return _no_trade(d, NO_TRADE_STALE_DATA)
    # Known structural provider delay (e.g. delayed public feeds) that is
    # material relative to the expiration horizon: a "5-minute" signal from a
    # ~15-minute-delayed feed is not a live signal, whatever its staleness
    # arithmetic says.
    if market_delay_seconds > 0:
        max_delay = h * 60.0 * cfg.data_quality.max_known_delay_fraction_of_horizon
        if market_delay_seconds > max_delay:
            d.warning_flags.append(
                f"KNOWN_PROVIDER_DELAY_{int(market_delay_seconds)}S"
            )
            return _no_trade(d, NO_TRADE_DATA_NOT_REAL_TIME)
    # Data age eats into the contract's life: if too little of the horizon
    # remains at the reference price, the analysis no longer describes the
    # contract that would actually be bought.
    if quality.data_age_seconds is not None:
        remaining = h * 60.0 - quality.data_age_seconds
        if remaining < h * 60.0 * rules.min_time_remaining_fraction:
            return _no_trade(d, NO_TRADE_TIME_TO_EXPIRY_TOO_SHORT)
    if quality.impossible_ohlc_count > 0 or quality.future_timestamp_count > 0:
        return _no_trade(d, NO_TRADE_ABNORMAL_DATA)
    if abnormal_market and rules.block_on_abnormal_range:
        return _no_trade(d, NO_TRADE_ABNORMAL_MARKET)
    if (
        quality.reference_price is not None
        and quality.reference_price <= cfg.data_quality.min_price
    ):
        return _no_trade(d, NO_TRADE_SUB_MINIMUM_PRICE)
    if quality.data_quality_score < cfg.data_quality.min_data_quality_score:
        reason = (
            NO_TRADE_MISSING_CANDLES
            if "MISSING_CANDLES" in quality.flags
            else NO_TRADE_LOW_DATA_QUALITY
        )
        return _no_trade(d, reason)
    if not liquidity.ok:
        d.warning_flags.extend(f"LIQUIDITY: {r}" for r in liquidity.reasons)
        return _no_trade(d, NO_TRADE_LOW_LIQUIDITY)
    if session.state == MARKET_UNKNOWN:
        return _no_trade(d, NO_TRADE_SESSION_NOT_SUPPORTED)
    if not session.allowed_for_analysis:
        return _no_trade(d, NO_TRADE_MARKET_CLOSED)
    expiry_session = classify_session_for_asset(expiry_utc, cfg.market_session, asset_type)
    if cfg.market_session.require_open_market and not expiry_session.allowed_for_analysis:
        # The contract would settle outside the allowed session; with
        # session-bound data there is no honest settlement print.
        return _no_trade(d, NO_TRADE_EXPIRY_BEYOND_SESSION)

    # ------------------------------------------------------------------
    # Evidence gates -> NO_TRADE with validity "valid"/"degraded" (the data
    # was fine; "do not trade" is the answer).
    # ------------------------------------------------------------------
    d.signal_validity = _data_validity(scores, quality, session, d.warning_flags)
    if scores.feature_coverage <= 0 or not scores.contributions:
        return _no_trade(d, NO_TRADE_NO_FEATURES, keep_validity=True)
    if scores.contradiction_ratio > rules.max_contradiction_ratio:
        return _no_trade(d, NO_TRADE_CONTRADICTORY, keep_validity=True)
    if abs(scores.directional_score) < rules.min_directional_margin:
        return _no_trade(d, NO_TRADE_SCORES_TOO_CLOSE, keep_validity=True)
    if scores.confidence_score < rules.min_confidence:
        return _no_trade(d, NO_TRADE_LOW_CONFIDENCE, keep_validity=True)

    d.signal = SIGNAL_CALL if scores.directional_score > 0 else SIGNAL_PUT
    d.signal_strength = _strength(scores, rules)
    d.no_trade_reason = None
    return d


def _contract_gate(d: SignalDecision, ctx: "ContractContext") -> Optional[SignalDecision]:
    """Apply catalog/contract gates. Returns a NO_TRADE decision or None.

    Research mode downgrades *availability/economics-unknown* gates to visible
    warnings (the row is explicitly a research row), but never bypasses a
    positively bad state: a closed contract, an unavailable expiration, an
    excluded OTC instrument, a below-minimum payout, or a quote mismatch is
    NO_TRADE in every mode.
    """
    if not ctx.availability_known:
        if ctx.research_mode:
            d.warning_flags += [WARN_NO_CATALOG, WARN_RESEARCH_MODE]
            return None
        return _no_trade(d, NO_TRADE_AVAILABILITY_UNKNOWN)

    if ctx.catalog_stale:
        if ctx.research_mode:
            d.warning_flags.append(NO_TRADE_CATALOG_STALE)
        else:
            return _no_trade(d, NO_TRADE_CATALOG_STALE)
    if not ctx.contract_available:
        return _no_trade(d, NO_TRADE_INSTRUMENT_NOT_AVAILABLE)
    if not ctx.contract_open:
        return _no_trade(d, NO_TRADE_CONTRACT_CLOSED)
    if ctx.otc_excluded:
        return _no_trade(d, NO_TRADE_OTC_EXCLUDED)
    if ctx.instrument is not None and ctx.instrument.is_otc:
        d.warning_flags.append(WARN_OTC_CONTRACT)
    if not ctx.expiration_available:
        return _no_trade(d, NO_TRADE_EXPIRATION_NOT_AVAILABLE)

    # Contract economics.
    if ctx.payout_ratio is None:
        if ctx.research_mode:
            d.warning_flags.append(NO_TRADE_PAYOUT_UNKNOWN)
        else:
            return _no_trade(d, NO_TRADE_PAYOUT_UNKNOWN)
    else:
        if ctx.payout_stale:
            if ctx.research_mode:
                d.warning_flags.append(NO_TRADE_PAYOUT_STALE)
            else:
                return _no_trade(d, NO_TRADE_PAYOUT_STALE)
        if ctx.payout_below_minimum:
            return _no_trade(d, NO_TRADE_PAYOUT_BELOW_MINIMUM)

    # Platform quote alignment (only checked when the catalog carries a quote).
    if ctx.quote_alignment_status == "stale":
        return _no_trade(d, NO_TRADE_PLATFORM_QUOTE_STALE)
    if ctx.quote_alignment_status == "mismatch":
        return _no_trade(d, NO_TRADE_QUOTE_MISMATCH)

    if ctx.research_mode:
        d.warning_flags.append(WARN_RESEARCH_MODE)
    return None


def _no_trade(d: SignalDecision, reason: str, *, keep_validity: bool = False) -> SignalDecision:
    d.signal = SIGNAL_NO_TRADE
    d.signal_strength = STRENGTH_NONE
    d.no_trade_reason = reason
    if not keep_validity:
        d.signal_validity = VALIDITY_INVALID
    return d


def _data_validity(
    scores: DirectionalScores,
    quality: SeriesQuality,
    session: SessionState,
    warning_flags: List[str],
) -> str:
    degraded = (
        scores.feature_coverage < 0.999
        or scores.vol_regime_factor < 1.0
        or quality.missing_count > 0
        or quality.duplicate_count > 0
        or quality.abnormal_return_count > 0
        or session.state not in (MARKET_OPEN, MARKET_247, MARKET_245)
        or bool(warning_flags)
    )
    return VALIDITY_DEGRADED if degraded else VALIDITY_VALID


def _strength(scores: DirectionalScores, rules) -> str:
    if (
        abs(scores.directional_score) >= rules.strong_min_directional
        and scores.confidence_score >= rules.strong_min_confidence
    ):
        return STRENGTH_STRONG
    if abs(scores.directional_score) >= rules.moderate_min_directional:
        return STRENGTH_MODERATE
    return STRENGTH_WEAK
