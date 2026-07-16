"""Feature-family engine: candles -> signed family scores per horizon.

Each directional family produces a signed score in [-1, +1] (+ = bullish
evidence) as the **mean of its available components**; a family with no
computable component is *missing* (recorded with a reason), never neutral-
filled. The volatility family is deliberately non-directional: it produces a
confidence multiplier (``vol_regime_factor``) and flags instead of a signed
score, because a wide bar says "conditions are unusual", not "price will go
up".

Component formulas (full derivations in BINARY_OPTIONS_METHODOLOGY.md):

momentum (windows scale with the horizon: w = horizon_bars × multiple)
    * normalized_return(w) for each configured multiple
    * consecutive_direction (streak / 5, signed)
    * range_position over the range window
trend
    * ema_gap    = clip((EMA_fast − EMA_slow) / ATR, ±1)
    * macd_hist  = clip(MACD_histogram / (0.5 · ATR), ±1)
    * sma_slope  = clip((SMA_now − SMA_horizon_bars_ago) / (ATR · horizon_bars/ema_fast), ±1)
mean_reversion (contrarian)
    * rsi_pressure: −(RSI − 70)/30 above 70, +(30 − RSI)/30 below 30, else 0
    * bollinger_reversion: −sign(z)·clip((|z| − 1)/(k − 1), 0, 1) when |z| > 1
volume (confirmation, signed by the last bar's direction)
    * clip((rel_volume − 1)/(high_rel_volume − 1), 0, 1) · sign(last bar move)
context (computed on the benchmark series with the same formulas)
    * normalized_return(w) for the configured context multiples
sentiment (optional; only horizons ≥ sentiment.min_horizon_minutes)
    * clip((sentiment_score − 50) / max_abs_tilt, ±1)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..config import BinaryOptionsConfig, FeatureParamsConfig
from ..data_providers.base import Candle
from . import indicators as ind

FAMILY_MOMENTUM = "momentum"
FAMILY_TREND = "trend"
FAMILY_MEAN_REVERSION = "mean_reversion"
FAMILY_VOLUME = "volume"
FAMILY_CONTEXT = "context"
FAMILY_SENTIMENT = "sentiment"

FLAG_VOL_ABNORMAL = "ABNORMAL_RANGE"
FLAG_VOL_EXPANSION = "VOLATILITY_EXPANSION"
FLAG_VOL_COMPRESSION = "VOLATILITY_COMPRESSION"


@dataclass
class FeatureBundle:
    """Everything the scoring layer needs for one (symbol, horizon) pair."""
    horizon_minutes: int
    horizon_bars: int
    family_scores: Dict[str, float] = field(default_factory=dict)
    # component name -> signed value, for drivers/conflicts and the outputs
    components: Dict[str, float] = field(default_factory=dict)
    # descriptive (unsigned) feature values worth reporting
    values: Dict[str, Optional[float]] = field(default_factory=dict)
    missing_families: Dict[str, str] = field(default_factory=dict)
    vol_regime_factor: float = 1.0
    flags: List[str] = field(default_factory=list)


def compute_features(
    candles: List[Candle],
    *,
    horizon_minutes: int,
    interval_minutes: int,
    cfg: BinaryOptionsConfig,
    benchmark_candles: Optional[List[Candle]] = None,
    sentiment_score: Optional[float] = None,
) -> FeatureBundle:
    fp = cfg.features
    horizon_bars = max(1, horizon_minutes // max(1, interval_minutes))
    bundle = FeatureBundle(horizon_minutes=horizon_minutes, horizon_bars=horizon_bars)

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    opens = [c.open for c in candles]
    volumes = [c.volume for c in candles]

    _momentum(bundle, closes, highs, lows, horizon_bars, fp)
    _trend(bundle, closes, highs, lows, horizon_bars, fp)
    _mean_reversion(bundle, closes, fp)
    _volume(bundle, closes, volumes, fp)
    _volatility(bundle, opens, highs, lows, closes, fp, cfg)
    _context(bundle, benchmark_candles, horizon_bars, fp)
    _sentiment(bundle, sentiment_score, horizon_minutes, cfg)

    return bundle


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

def _family_from(
    bundle: FeatureBundle, family: str, parts: Dict[str, Optional[float]], min_parts: int = 1
) -> None:
    available = {k: v for k, v in parts.items() if v is not None}
    if len(available) < min_parts:
        bundle.missing_families[family] = (
            f"insufficient data: {len(available)}/{len(parts)} components computable"
        )
        return
    for k, v in available.items():
        bundle.components[f"{family}.{k}"] = round(v, 4)
    bundle.family_scores[family] = round(
        sum(available.values()) / len(available), 4
    )


def _momentum(bundle, closes, highs, lows, horizon_bars, fp: FeatureParamsConfig) -> None:
    vol_window = max(fp.range_window_bars, horizon_bars * 2)
    parts: Dict[str, Optional[float]] = {}
    for m in fp.return_window_multiples:
        parts[f"return_{m}x"] = ind.normalized_return(closes, horizon_bars * m, vol_window)
    parts["consecutive"] = ind.consecutive_direction(closes)
    parts["range_position"] = ind.range_position(
        highs, lows, closes[-1], fp.range_window_bars
    ) if closes else None
    _family_from(bundle, FAMILY_MOMENTUM, parts, min_parts=2)
    for m in fp.return_window_multiples:
        bundle.values[f"return_{m}x_horizon"] = ind.window_return(closes, horizon_bars * m)


def _trend(bundle, closes, highs, lows, horizon_bars, fp: FeatureParamsConfig) -> None:
    atr_v = ind.atr(highs, lows, closes, fp.atr_period_bars)
    bundle.values["atr"] = atr_v
    parts: Dict[str, Optional[float]] = {"ema_gap": None, "macd_hist": None, "sma_slope": None}
    if atr_v and atr_v > 0:
        ema_fast = ind.ema(closes, fp.ema_fast_bars)
        ema_slow = ind.ema(closes, fp.ema_slow_bars)
        if ema_fast is not None and ema_slow is not None:
            parts["ema_gap"] = _clip((ema_fast - ema_slow) / atr_v)
        hist = ind.macd_histogram(
            closes, fp.macd_fast_bars, fp.macd_slow_bars, fp.macd_signal_bars
        )
        if hist is not None:
            parts["macd_hist"] = _clip(hist / (0.5 * atr_v))
        sma_now = ind.sma(closes, fp.ema_fast_bars)
        sma_then = (
            ind.sma(closes[:-horizon_bars], fp.ema_fast_bars)
            if len(closes) > horizon_bars else None
        )
        if sma_now is not None and sma_then is not None:
            denom = atr_v * max(1.0, horizon_bars / fp.ema_fast_bars)
            parts["sma_slope"] = _clip((sma_now - sma_then) / denom)
    _family_from(bundle, FAMILY_TREND, parts, min_parts=2)


def _mean_reversion(bundle, closes, fp: FeatureParamsConfig) -> None:
    rsi_v = ind.rsi(closes, fp.rsi_period_bars)
    z = ind.bollinger_z(closes, fp.bollinger_period_bars)
    bundle.values["rsi"] = rsi_v
    bundle.values["bollinger_z"] = z
    parts: Dict[str, Optional[float]] = {"rsi_pressure": None, "bollinger_reversion": None}
    if rsi_v is not None:
        if rsi_v >= fp.rsi_overbought:
            parts["rsi_pressure"] = -_clip((rsi_v - fp.rsi_overbought) / (100.0 - fp.rsi_overbought), 0, 1)
        elif rsi_v <= fp.rsi_oversold:
            parts["rsi_pressure"] = _clip((fp.rsi_oversold - rsi_v) / fp.rsi_oversold, 0, 1)
        else:
            parts["rsi_pressure"] = 0.0
    if z is not None:
        k = max(1.001, fp.bollinger_k)
        if abs(z) > 1.0:
            parts["bollinger_reversion"] = -math.copysign(
                _clip((abs(z) - 1.0) / (k - 1.0), 0, 1), z
            )
        else:
            parts["bollinger_reversion"] = 0.0
    _family_from(bundle, FAMILY_MEAN_REVERSION, parts, min_parts=1)


def _volume(bundle, closes, volumes, fp: FeatureParamsConfig) -> None:
    rel = ind.relative_volume(volumes, fp.volume_window_bars)
    bundle.values["relative_volume"] = rel
    parts: Dict[str, Optional[float]] = {"volume_confirmation": None}
    if rel is not None and len(closes) >= 2:
        direction = 0.0
        if closes[-1] > closes[-2]:
            direction = 1.0
        elif closes[-1] < closes[-2]:
            direction = -1.0
        strength = _clip((rel - 1.0) / max(1e-9, fp.high_relative_volume - 1.0), 0, 1)
        parts["volume_confirmation"] = direction * strength
    _family_from(bundle, FAMILY_VOLUME, parts, min_parts=1)


def _volatility(bundle, opens, highs, lows, closes, fp: FeatureParamsConfig,
                cfg: BinaryOptionsConfig) -> None:
    """Non-directional: sets vol_regime_factor + flags + descriptive values."""
    ratio = ind.range_ratio(highs, lows, fp.range_window_bars)
    bundle.values["range_ratio"] = ratio
    if closes:
        btr = ind.body_to_range(opens[-1], highs[-1], lows[-1], closes[-1])
        bundle.values["body_to_range"] = btr
        wicks = ind.wick_proportions(opens[-1], highs[-1], lows[-1], closes[-1])
        if wicks:
            bundle.values["upper_wick"], bundle.values["lower_wick"] = (
                round(wicks[0], 4), round(wicks[1], 4)
            )
    floor = cfg.signal_rules.vol_regime_floor
    factor = 1.0
    if ratio is not None:
        if ratio >= fp.abnormal_range_multiple:
            factor = floor
            bundle.flags.append(FLAG_VOL_ABNORMAL)
        elif ratio >= fp.expansion_range_ratio:
            span = max(1e-9, fp.abnormal_range_multiple - fp.expansion_range_ratio)
            factor = max(floor, 1.0 - 0.5 * (ratio - fp.expansion_range_ratio) / span)
            bundle.flags.append(FLAG_VOL_EXPANSION)
        elif ratio <= fp.compression_range_ratio:
            factor = max(floor, 0.85)
            bundle.flags.append(FLAG_VOL_COMPRESSION)
    bundle.vol_regime_factor = round(factor, 4)


def _context(bundle, benchmark_candles, horizon_bars, fp: FeatureParamsConfig) -> None:
    if not benchmark_candles:
        bundle.missing_families[FAMILY_CONTEXT] = "benchmark series unavailable"
        return
    closes = [c.close for c in benchmark_candles]
    vol_window = max(fp.range_window_bars, horizon_bars * 2)
    parts: Dict[str, Optional[float]] = {}
    for m in fp.context_return_window_multiples:
        parts[f"benchmark_return_{m}x"] = ind.normalized_return(
            closes, horizon_bars * m, vol_window
        )
    _family_from(bundle, FAMILY_CONTEXT, parts, min_parts=1)


def _sentiment(bundle, sentiment_score, horizon_minutes, cfg: BinaryOptionsConfig) -> None:
    s = cfg.sentiment
    if not s.enabled:
        bundle.missing_families[FAMILY_SENTIMENT] = "sentiment disabled"
        return
    if horizon_minutes < s.min_horizon_minutes:
        bundle.missing_families[FAMILY_SENTIMENT] = (
            f"horizon {horizon_minutes}m below sentiment.min_horizon_minutes "
            f"({s.min_horizon_minutes}m) -- stale news must not drive short signals"
        )
        return
    if sentiment_score is None:
        bundle.missing_families[FAMILY_SENTIMENT] = "no sentiment score available"
        return
    bundle.values["sentiment_score_input"] = sentiment_score
    tilt = _clip((float(sentiment_score) - 50.0) / max(1e-9, s.max_abs_tilt))
    _family_from(bundle, FAMILY_SENTIMENT, {"news_tilt": tilt}, min_parts=1)


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))
