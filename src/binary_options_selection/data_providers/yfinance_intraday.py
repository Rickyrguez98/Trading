"""Default free intraday provider backed by yfinance (Yahoo Finance).

Honesty notes (documented in BINARY_OPTIONS_DATA_SOURCES.md):

* Yahoo intraday data is **delayed** (typically ~15 minutes for many venues)
  and unofficial — no SLA. The freshness fields let the staleness gate react
  instead of pretending the tape is live.
* yfinance limits intraday history (1m: ~7 days; other intraday intervals:
  ~60 days). The provider requests only what the pipeline needs.
* Only provider-native intervals are requested. No resampling to finer
  intervals, no daily-as-intraday.
* In-progress candles are returned flagged ``complete=False`` and are **never
  written to the cache** — a partially formed bar must not be served later as
  if it were final.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import errors as err
from ..config import SUPPORTED_INTERVALS
from ..utils.timeutils import (
    candle_close_time,
    candle_is_complete,
    iso_utc,
    parse_utc,
    utc_now,
)
from .base import Candle, IntradayCandlesProvider, IntradaySeries

logger = logging.getLogger(__name__)

# Yahoo caps how far back each intraday interval reaches.
_MAX_LOOKBACK_DAYS = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 60, "1h": 60}
# Regular-session minutes per U.S. trading day (used to size the period request).
_REGULAR_MINUTES_PER_DAY = 390


def _provider_symbol(symbol: str) -> str:
    """Canonical dot class-share notation -> Yahoo hyphen notation (BRK.B -> BRK-B)."""
    return symbol.strip().upper().replace(".", "-")


def _period_days(bars: int, interval_minutes: int, interval: str) -> int:
    minutes_needed = bars * interval_minutes
    days = max(1, math.ceil(minutes_needed / _REGULAR_MINUTES_PER_DAY) + 2)  # weekend/gap headroom
    return min(days, _MAX_LOOKBACK_DAYS.get(interval, 60))


class YFinanceIntradayProvider(IntradayCandlesProvider):
    name = "yfinance"
    cache_namespace = "intraday_yfinance"

    def fetch(
        self,
        symbol: str,
        *,
        interval: str,
        bars: int,
        include_extended_hours: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> IntradaySeries:
        now = utc_now()
        psym = _provider_symbol(symbol)
        series = IntradaySeries(
            symbol=symbol,
            provider_symbol=psym,
            interval=interval,
            interval_seconds=SUPPORTED_INTERVALS.get(interval, 0) * 60,
            provider=self.name,
            requested_at=iso_utc(now),
            session_coverage="extended" if include_extended_hours else "regular",
        )
        if interval not in SUPPORTED_INTERVALS:
            series.status = "error"
            series.error_type = err.UNSUPPORTED_INTERVAL
            series.error = f"interval {interval!r} is not a supported intraday interval"
            series.data_source = "unavailable"
            return series

        cache_key = f"{psym}:{interval}:{int(bars)}:{int(include_extended_hours)}"
        cached = self.cache.get(self.cache_namespace, cache_key, ttl=ttl_seconds)
        if cached is not None:
            self._apply_payload(series, cached, now)
            series.data_source = "fresh_cache"
            return series

        try:
            payload = self._fetch_live(psym, interval, bars, include_extended_hours)
        except Exception as exc:  # noqa: BLE001 - classified, never re-raised
            series.status = "error"
            series.error_type = err.classify_exception(exc)
            series.error = f"{type(exc).__name__}: {exc}"
            series.data_source = "unavailable"
            logger.warning("yfinance intraday fetch failed for %s: %s", symbol, series.error)
            return series

        if not payload["candles"]:
            series.status = "empty"
            series.error_type = err.NO_INTRADAY_DATA
            series.error = "provider returned no intraday candles"
            series.data_source = "unavailable"
            return series

        self._apply_payload(series, payload, now)
        series.data_source = "live"
        # Cache only candles that were complete at fetch time: a partially
        # formed bar must never be served later as if it were final.
        cacheable = dict(payload)
        cacheable["candles"] = [
            c for c in payload["candles"]
            if candle_is_complete(parse_utc(c["start"]), series.interval_seconds, now)
        ]
        self.cache.set(self.cache_namespace, cache_key, cacheable)
        return series

    # ------------------------------------------------------------------
    def _fetch_live(
        self, psym: str, interval: str, bars: int, include_extended_hours: bool
    ) -> Dict[str, Any]:
        import pandas as pd  # noqa: F401  (yfinance returns DataFrames)
        import yfinance as yf

        interval_minutes = SUPPORTED_INTERVALS[interval]
        period = f"{_period_days(bars, interval_minutes, interval)}d"
        self.rate_limiter.acquire()
        # auto_adjust must be explicit: we record adjusted status honestly.
        df = yf.Ticker(psym).history(
            period=period,
            interval="60m" if interval == "1h" else interval,
            prepost=include_extended_hours,
            auto_adjust=False,
        )
        candles: List[Dict[str, Any]] = []
        tz_name: Optional[str] = None
        if df is not None and not df.empty:
            tz_name = str(df.index.tz) if df.index.tz is not None else None
            for ts, row in df.iterrows():
                if ts.tzinfo is None:
                    # A naive intraday timestamp is ambiguous; skip rather than guess.
                    continue
                vals = [row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")]
                if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in vals):
                    continue  # unusable bar; the gap is counted by validation
                candles.append({
                    "start": ts.tz_convert(timezone.utc).isoformat(),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume") or 0.0),
                })
        candles = candles[-int(bars):]
        return {"candles": candles, "exchange_timezone": tz_name, "adjusted": False}

    # ------------------------------------------------------------------
    def _apply_payload(
        self, series: IntradaySeries, payload: Dict[str, Any], now: datetime
    ) -> None:
        out: List[Candle] = []
        for c in payload.get("candles", []):
            start = parse_utc(c["start"])
            out.append(Candle(
                start=start,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c.get("volume", 0.0)),
                complete=candle_is_complete(start, series.interval_seconds, now),
            ))
        out.sort(key=lambda c: c.start)
        series.candles = out
        series.exchange_timezone = payload.get("exchange_timezone")
        series.adjusted = payload.get("adjusted")
        if out:
            series.latest_candle_start = iso_utc(out[-1].start)
            last_complete = series.latest_complete()
            if last_complete is not None:
                series.latest_candle_close = iso_utc(
                    candle_close_time(last_complete.start, series.interval_seconds)
                )
