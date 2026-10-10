"""Intraday OHLCV provider interface and record schemas.

The record schema carries every honesty field the pipeline needs to report a
series' provenance and limitations: provider status, error taxonomy, cache
provenance, request/latest timestamps, freshness, missing/duplicate counts,
and adjusted-vs-unadjusted status. Providers never fabricate, forward-fill,
interpolate, or impute candles — gaps stay gaps and are counted downstream.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from trading_common.cache import Cache
from trading_common.rate_limiter import RateLimiter

from ..utils.timeutils import iso_utc, utc_now

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """One completed (or explicitly in-progress) OHLCV bar.

    ``start`` is the aware-UTC open time of the bar; the bar covers
    ``[start, start + interval)``. ``complete`` is set by the fetch layer from
    the deterministic boundary rule, never guessed.
    """
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True


@dataclass
class IntradaySeries:
    """A fetched intraday candle series plus its full provenance."""
    symbol: str                                # canonical symbol (e.g. BRK.B)
    provider_symbol: Optional[str] = None      # what was actually sent (BRK-B)
    interval: str = "5m"
    interval_seconds: int = 300
    candles: List[Candle] = field(default_factory=list)

    # Provider outcome
    provider: Optional[str] = None
    status: str = "ok"                         # "ok" | "empty" | "error"
    error: Optional[str] = None
    error_type: Optional[str] = None           # data_providers.errors constant
    # Provenance: "live" | "fresh_cache" | "unavailable". Stale cache is never
    # served as data in this module -- intraday staleness invalidates a signal.
    data_source: Optional[str] = None

    # Data characteristics
    adjusted: Optional[bool] = None            # split/dividend adjusted?
    exchange_timezone: Optional[str] = None    # as reported by the provider
    session_coverage: Optional[str] = None     # "regular" | "extended" | None

    # Timing / freshness (aware-UTC ISO strings for JSON friendliness)
    requested_at: Optional[str] = None
    latest_candle_start: Optional[str] = None
    latest_candle_close: Optional[str] = None

    # Integrity counters (filled by validation.candles.assess_series)
    missing_count: int = 0
    duplicate_count: int = 0

    def latest_complete(self) -> Optional[Candle]:
        for c in reversed(self.candles):
            if c.complete:
                return c
        return None

    def to_meta(self) -> Dict[str, Any]:
        """JSON-friendly provenance summary (no candle payload)."""
        return {
            "symbol": self.symbol,
            "provider_symbol": self.provider_symbol,
            "provider": self.provider,
            "interval": self.interval,
            "candle_count": len(self.candles),
            "status": self.status,
            "error_type": self.error_type,
            "error": self.error,
            "data_source": self.data_source,
            "adjusted": self.adjusted,
            "exchange_timezone": self.exchange_timezone,
            "session_coverage": self.session_coverage,
            "requested_at": self.requested_at,
            "latest_candle_close": self.latest_candle_close,
            "missing_count": self.missing_count,
            "duplicate_count": self.duplicate_count,
        }


class IntradayCandlesProvider(ABC):
    """Base class for intraday candle providers.

    Concrete providers receive a :class:`Cache` and a :class:`RateLimiter`
    (same wiring pattern as the equity module) and implement :meth:`fetch`.
    The cache TTL is **interval-aware** and injected per call by the pipeline
    via ``ttl_seconds`` so 5-minute data is never served hours late.
    """

    name: str = "base"
    cache_namespace: str = "intraday"

    def __init__(
        self,
        cache: Optional[Cache] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.cache = cache or Cache(enabled=False)
        self.rate_limiter = rate_limiter or RateLimiter(0.0)

    @abstractmethod
    def fetch(
        self,
        symbol: str,
        *,
        interval: str,
        bars: int,
        include_extended_hours: bool = False,
        ttl_seconds: Optional[int] = None,
        provider_symbol: Optional[str] = None,
    ) -> IntradaySeries:
        """Return an :class:`IntradaySeries`; never raise on missing data.

        Implementations must:
        * return candles sorted by start time, timestamps aware-UTC;
        * mark the last candle ``complete=False`` when its window has not
          elapsed (the pipeline decides whether to drop it);
        * report failures via ``status`` / ``error_type`` instead of raising;
        * never resample coarse data to a finer interval and never return
          daily bars for an intraday request.
        """

    # ------------------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return iso_utc(utc_now())
