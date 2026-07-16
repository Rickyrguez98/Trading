"""Candle-series integrity validation and data-quality scoring.

Checks (all machine-readable, none silently "fixed"):
ordering, duplicate timestamps, missing bars, impossible OHLC relationships,
zero/negative prices, negative volume, future timestamps (look-ahead guard),
abnormal single-bar returns, zero-volume ratio, staleness of the latest
completed candle, incomplete last candle, and insufficient history.

Missing-bar counting is session-aware in a conservative way: a gap between
consecutive candles is counted as missing bars only when it is shorter than
``_SESSION_BREAK_SECONDS`` (4 h); longer gaps are treated as session breaks
(overnight/weekend), not data loss. This under-counts pathological multi-hour
outages rather than over-counting weekends — the staleness gate catches the
former.

The ``data_quality_score`` formula (0–100, documented in
BINARY_OPTIONS_METHODOLOGY.md):

    score = 100
            − 60·missing_ratio − 40·duplicate_ratio
            − 10·out_of_order (cap 30) − 15·impossible_ohlc (cap 45)
            − 10·abnormal_return_bars (cap 30)
            − 20·excess_zero_volume_ratio
    then capped at 40 if the series is stale, at 30 if history is insufficient,
    clamped to [0, 100].
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ..config import BinaryOptionsConfig
from ..data_providers import errors as err
from ..data_providers.base import Candle, IntradaySeries
from ..utils.timeutils import candle_close_time, data_age_seconds, ensure_utc

_SESSION_BREAK_SECONDS = 4 * 3600
_FUTURE_TOLERANCE_SECONDS = 60.0

FLAG_STALE_DATA = "STALE_INTRADAY_DATA"
FLAG_INCOMPLETE_LAST = "INCOMPLETE_LAST_CANDLE_DROPPED"
FLAG_INCOMPLETE_USED = "INCOMPLETE_CANDLE_USED"
FLAG_MISSING_CANDLES = "MISSING_CANDLES"
FLAG_DUPLICATES = "DUPLICATE_CANDLES"
FLAG_OUT_OF_ORDER = "OUT_OF_ORDER_TIMESTAMPS"
FLAG_IMPOSSIBLE_OHLC = "IMPOSSIBLE_OHLC"
FLAG_BAD_PRICE = "NONPOSITIVE_PRICE"
FLAG_NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
FLAG_FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
FLAG_ABNORMAL_RETURN = "ABNORMAL_BAR_RETURN"
FLAG_HIGH_ZERO_VOLUME = "HIGH_ZERO_VOLUME_RATIO"
FLAG_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


@dataclass
class SeriesQuality:
    symbol: str
    usable: bool = False                 # enough clean data to compute features
    stale: bool = False
    insufficient_history: bool = False
    data_quality_score: float = 0.0
    data_age_seconds: Optional[float] = None
    latest_complete_close: Optional[datetime] = None
    reference_price: Optional[float] = None
    candles: List[Candle] = field(default_factory=list)  # cleaned, complete bars
    flags: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    missing_count: int = 0
    duplicate_count: int = 0
    out_of_order_count: int = 0
    impossible_ohlc_count: int = 0
    abnormal_return_count: int = 0
    future_timestamp_count: int = 0
    zero_volume_ratio: float = 0.0
    error_type: Optional[str] = None


def assess_series(
    series: IntradaySeries,
    cfg: BinaryOptionsConfig,
    now: datetime,
) -> SeriesQuality:
    """Validate a fetched series and produce cleaned candles + a quality score."""
    q = SeriesQuality(symbol=series.symbol)
    now = ensure_utc(now)
    dq = cfg.data_quality
    interval_s = series.interval_seconds or cfg.candles.interval_seconds

    if series.status != "ok" or not series.candles:
        q.error_type = series.error_type or err.NO_INTRADAY_DATA
        q.issues.append(series.error or "no candles returned")
        return q

    candles = list(series.candles)

    # --- ordering / duplicates / future stamps (before any cleaning) ---
    seen = set()
    prev_start: Optional[datetime] = None
    deduped: List[Candle] = []
    for c in candles:
        start = ensure_utc(c.start)
        if (start - now).total_seconds() > _FUTURE_TOLERANCE_SECONDS:
            q.future_timestamp_count += 1
            continue  # a future bar can only be corrupt; never score on it
        if prev_start is not None and start < prev_start:
            q.out_of_order_count += 1
        prev_start = max(prev_start, start) if prev_start else start
        key = start.isoformat()
        if key in seen:
            q.duplicate_count += 1
            continue
        seen.add(key)
        deduped.append(c)
    deduped.sort(key=lambda c: ensure_utc(c.start))

    # --- drop the in-progress candle unless explicitly allowed ---
    if deduped and not deduped[-1].complete:
        if cfg.candles.allow_incomplete_last_candle:
            q.flags.append(FLAG_INCOMPLETE_USED)
        else:
            deduped = deduped[:-1]
            q.flags.append(FLAG_INCOMPLETE_LAST)

    # --- per-candle integrity ---
    clean: List[Candle] = []
    zero_volume = 0
    prev_close: Optional[float] = None
    for c in deduped:
        prices = (c.open, c.high, c.low, c.close)
        if any(p is None or p <= 0 for p in prices):
            q.impossible_ohlc_count += 1
            q.issues.append(f"non-positive price at {c.start.isoformat()}")
            continue
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.high < c.low:
            q.impossible_ohlc_count += 1
            q.issues.append(f"impossible OHLC at {c.start.isoformat()}")
            continue
        if c.volume < 0:
            q.issues.append(f"negative volume at {c.start.isoformat()}")
            q.flags.append(FLAG_NEGATIVE_VOLUME)
            continue
        if c.volume == 0:
            zero_volume += 1
        if prev_close is not None and prev_close > 0:
            if abs(c.close / prev_close - 1.0) > dq.max_abnormal_bar_return:
                q.abnormal_return_count += 1
        prev_close = c.close
        clean.append(c)

    # --- missing bars (session-aware, conservative) ---
    missing = 0
    for a, b in zip(clean, clean[1:]):
        delta = (ensure_utc(b.start) - ensure_utc(a.start)).total_seconds()
        if interval_s < delta < _SESSION_BREAK_SECONDS:
            missing += int(round(delta / interval_s)) - 1
    q.missing_count = missing
    series.missing_count = missing
    series.duplicate_count = q.duplicate_count

    q.candles = clean
    n = len(clean)
    q.zero_volume_ratio = (zero_volume / n) if n else 1.0

    # --- staleness / history ---
    if clean:
        q.latest_complete_close = candle_close_time(clean[-1].start, interval_s)
        q.reference_price = clean[-1].close
        q.data_age_seconds = data_age_seconds(q.latest_complete_close, now)
        max_age = cfg.candles.max_staleness_intervals * interval_s
        q.stale = bool(q.data_age_seconds is not None and q.data_age_seconds > max_age)
    else:
        q.stale = True
    q.insufficient_history = n < cfg.candles.required_history_bars

    # --- flags ---
    expected = n + missing
    missing_ratio = (missing / expected) if expected else 1.0
    duplicate_ratio = (q.duplicate_count / max(1, n + q.duplicate_count))
    if q.stale:
        q.flags.append(FLAG_STALE_DATA)
        q.error_type = q.error_type or err.STALE_INTRADAY_DATA
    if missing_ratio > dq.max_missing_ratio:
        q.flags.append(FLAG_MISSING_CANDLES)
    if duplicate_ratio > dq.max_duplicate_ratio:
        q.flags.append(FLAG_DUPLICATES)
    if q.out_of_order_count:
        q.flags.append(FLAG_OUT_OF_ORDER)
    if q.impossible_ohlc_count:
        q.flags.append(FLAG_IMPOSSIBLE_OHLC)
        q.error_type = q.error_type or err.CORRUPT_CANDLE_DATA
    if q.abnormal_return_count:
        q.flags.append(FLAG_ABNORMAL_RETURN)
    if q.future_timestamp_count:
        q.flags.append(FLAG_FUTURE_TIMESTAMP)
    if q.zero_volume_ratio > cfg.liquidity.max_zero_volume_ratio:
        q.flags.append(FLAG_HIGH_ZERO_VOLUME)
    if q.insufficient_history:
        q.flags.append(FLAG_INSUFFICIENT_HISTORY)
        q.error_type = q.error_type or err.INSUFFICIENT_HISTORY
    if clean and any(p <= dq.min_price for p in (q.reference_price,) if p is not None):
        q.flags.append(FLAG_BAD_PRICE)

    # --- score (formula documented in the module docstring) ---
    score = 100.0
    score -= 60.0 * min(1.0, missing_ratio)
    score -= 40.0 * min(1.0, duplicate_ratio)
    score -= min(30.0, 10.0 * q.out_of_order_count)
    score -= min(45.0, 15.0 * q.impossible_ohlc_count)
    score -= min(30.0, 10.0 * q.abnormal_return_count)
    allowed_zero = cfg.liquidity.max_zero_volume_ratio
    if q.zero_volume_ratio > allowed_zero and allowed_zero < 1.0:
        score -= 20.0 * (q.zero_volume_ratio - allowed_zero) / (1.0 - allowed_zero)
    if q.stale:
        score = min(score, 40.0)
    if q.insufficient_history:
        score = min(score, 30.0)
    q.data_quality_score = max(0.0, min(100.0, round(score, 2)))

    q.usable = (
        n > 0
        and not q.insufficient_history
        and q.data_quality_score >= dq.min_data_quality_score
    )
    return q
