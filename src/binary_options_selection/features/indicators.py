"""Pure intraday indicator functions.

Every function:
* takes plain ``Sequence[float]`` (or candle fields) — no pandas dependency;
* returns ``None`` when its minimum-data requirement is not met (the caller
  records the family as missing; values are **never** silently imputed);
* is documented with its formula here and in BINARY_OPTIONS_METHODOLOGY.md;
* uses only past data relative to the last element (no look-ahead).
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def sma(values: Sequence[float], period: int) -> Optional[float]:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_series(values: Sequence[float], period: int) -> Optional[List[float]]:
    """Exponential moving average series, seeded with the SMA of the first
    ``period`` values; multiplier k = 2 / (period + 1)."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out = [seed]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(closes: Sequence[float], period: int) -> Optional[float]:
    """Wilder's RSI: 100 − 100 / (1 + avg_gain / avg_loss), smoothed with
    alpha = 1/period. Needs ``period + 1`` closes."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        change = cur - prev
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_histogram(
    closes: Sequence[float], fast: int, slow: int, signal: int
) -> Optional[float]:
    """MACD histogram = (EMA_fast − EMA_slow) − EMA_signal(MACD line)."""
    if len(closes) < slow + signal:
        return None
    fast_s = ema_series(closes, fast)
    slow_s = ema_series(closes, slow)
    if not fast_s or not slow_s:
        return None
    # Align: ema_series(period) starts at index period-1 of the input.
    offset = slow - fast
    macd_line = [f - s for f, s in zip(fast_s[offset:], slow_s)]
    signal_s = ema_series(macd_line, signal)
    if not signal_s:
        return None
    return macd_line[-1] - signal_s[-1]


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> Optional[float]:
    """Wilder's Average True Range. TR = max(H−L, |H−C_prev|, |L−C_prev|)."""
    n = len(closes)
    if period <= 0 or n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def bollinger_z(closes: Sequence[float], period: int) -> Optional[float]:
    """z = (close − SMA) / rolling population stdev over ``period`` closes."""
    if period <= 1 or len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((v - mean) ** 2 for v in window) / period
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (closes[-1] - mean) / std


def window_return(closes: Sequence[float], bars: int) -> Optional[float]:
    """Fractional return over the last ``bars`` bars: close[-1]/close[-1-bars] − 1."""
    if bars <= 0 or len(closes) < bars + 1:
        return None
    base = closes[-1 - bars]
    if base <= 0:
        return None
    return closes[-1] / base - 1.0


def per_bar_return_std(closes: Sequence[float], window: int) -> Optional[float]:
    """Population stdev of one-bar fractional returns over the last ``window`` bars."""
    if window <= 1 or len(closes) < window + 1:
        return None
    rets = [
        closes[i] / closes[i - 1] - 1.0
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


def normalized_return(closes: Sequence[float], bars: int, vol_window: int) -> Optional[float]:
    """Volatility-normalized window return, clipped to [-1, 1]:

        z = window_return(bars) / (per_bar_return_std · sqrt(bars))
        score = clip(z / 2, -1, +1)          (a 2σ move saturates the score)
    """
    ret = window_return(closes, bars)
    vol = per_bar_return_std(closes, vol_window)
    if ret is None or vol is None:
        return None
    denom = vol * math.sqrt(bars)
    if denom <= 0:
        return 0.0 if ret == 0 else math.copysign(1.0, ret)
    return _clip(ret / denom / 2.0)


def range_position(highs: Sequence[float], lows: Sequence[float],
                   close: float, window: int) -> Optional[float]:
    """Position of the close inside the rolling high-low range, in [-1, +1]:
    +1 at the window high, −1 at the window low."""
    if window <= 0 or len(highs) < window or len(lows) < window:
        return None
    hi = max(highs[-window:])
    lo = min(lows[-window:])
    if hi <= lo:
        return 0.0
    return _clip(2.0 * (close - lo) / (hi - lo) - 1.0)


def consecutive_direction(closes: Sequence[float], cap: int = 5) -> Optional[float]:
    """Signed count of consecutive same-direction closes scaled by ``cap``:
    +1.0 after ``cap`` consecutive up closes, −1.0 after ``cap`` down closes."""
    if len(closes) < 2:
        return None
    direction = 0
    count = 0
    for prev, cur in zip(reversed(closes[:-1]), reversed(closes[1:])):
        step = 1 if cur > prev else (-1 if cur < prev else 0)
        if step == 0:
            break
        if direction == 0:
            direction = step
            count = 1
        elif step == direction:
            count += 1
        else:
            break
    return _clip(direction * count / float(cap))


def relative_volume(volumes: Sequence[float], window: int) -> Optional[float]:
    """Last bar volume / rolling mean volume over the prior ``window`` bars."""
    if window <= 0 or len(volumes) < window + 1:
        return None
    base = volumes[-1 - window:-1]
    mean = sum(base) / len(base)
    if mean <= 0:
        return None
    return volumes[-1] / mean


def body_to_range(open_: float, high: float, low: float, close: float) -> Optional[float]:
    """|close − open| / (high − low); None on a zero-range bar."""
    rng = high - low
    if rng <= 0:
        return None
    return abs(close - open_) / rng


def wick_proportions(open_: float, high: float, low: float, close: float):
    """(upper_wick, lower_wick) as fractions of the bar range; None on zero range."""
    rng = high - low
    if rng <= 0:
        return None
    upper = (high - max(open_, close)) / rng
    lower = (min(open_, close) - low) / rng
    return upper, lower


def range_ratio(highs: Sequence[float], lows: Sequence[float], window: int) -> Optional[float]:
    """Current bar range / median bar range over the prior ``window`` bars."""
    if window <= 0 or len(highs) < window + 1 or len(lows) < window + 1:
        return None
    ranges = [h - low for h, low in zip(highs[-1 - window:-1], lows[-1 - window:-1])]
    ranges = [r for r in ranges if r >= 0]
    if not ranges:
        return None
    med = _median(ranges)
    cur = highs[-1] - lows[-1]
    if med <= 0:
        return None
    return cur / med


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))
