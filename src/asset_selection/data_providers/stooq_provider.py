"""Stooq prices backup provider (free, keyless).

Stooq exposes a CSV endpoint that needs no API key:

    https://stooq.com/q/d/l/?s=aapl.us&i=d

It returns daily OHLCV rows (``Date,Open,High,Low,Close,Volume``). US symbols
take a ``.us`` market suffix and spell class shares with a hyphen (``brk-b.us``).
This makes Stooq a practical **fallback** for the liquidity/price-context metrics
the funnel needs (last close, average dollar volume, recent return, a volatility
proxy) when yfinance is blocked or rate-limited.

We keep the same :class:`PriceSnapshot` contract and error taxonomy as the
yfinance provider so the fallback wrapper can treat them interchangeably.
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import fields as _dc_fields
from typing import Any, Dict

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from ..utils.validation import coerce_float
from . import errors as err
from .base import PriceSnapshot, PricesProvider
from .symbols import likely_no_data_reason, to_provider_symbol, was_remapped

logger = logging.getLogger(__name__)

_STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
_PRICE_FIELDS = {f.name for f in _dc_fields(PriceSnapshot)}


def _compat(cached: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in cached.items() if k in _PRICE_FIELDS}


def _stooq_symbol(canonical: str) -> str:
    """Canonical -> Stooq symbol, e.g. AAPL -> aapl.us, BRK.B -> brk-b.us."""
    base = to_provider_symbol(canonical.strip().upper(), "stooq").lower()
    return f"{base}.us"


class StooqPricesProvider(PricesProvider):
    name = "stooq"

    def cache_identifier(self, ticker: str, lookback_days: int = 90) -> str:
        return f"{_stooq_symbol(ticker)}:{lookback_days}"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _download_csv(self, stooq_symbol: str) -> pd.DataFrame:
        # Imported lazily so the dependency is only needed when Stooq is used.
        import urllib.request

        self.rate_limiter.acquire()
        url = _STOOQ_URL.format(symbol=stooq_symbol)
        req = urllib.request.Request(url, headers={"User-Agent": "asset-selection/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed host
            raw = resp.read().decode("utf-8", errors="replace")
        text = raw.strip()
        # Stooq returns the literal "No data" (or a bare header) for unknown or
        # uncovered symbols -- treat that as an empty frame, not an error.
        if not text or text.lower().startswith("no data"):
            return pd.DataFrame()
        df = pd.read_csv(io.StringIO(raw))
        if df.empty or "Close" not in df.columns:
            return pd.DataFrame()
        return df

    def fetch(self, ticker: str, lookback_days: int = 90) -> PriceSnapshot:
        ticker = ticker.strip().upper()
        provider_symbol = _stooq_symbol(ticker)
        cache_id = f"{provider_symbol}:{lookback_days}"

        cached = self._cache_get(cache_id)
        if cached is not None:
            cached.setdefault("provider_symbol", provider_symbol)
            snap = PriceSnapshot(**_compat(cached))
            snap.data_source = "fresh_cache"
            return snap

        snap = PriceSnapshot(
            ticker=ticker, lookback_days=lookback_days, source=self.name,
            provider_symbol=provider_symbol, data_source="live",
        )

        try:
            hist = self._download_csv(provider_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stooq price fetch errored for %s (as %s): %s",
                ticker, provider_symbol, exc,
            )
            snap.status = "error"
            snap.error = f"{type(exc).__name__}: {exc}"
            snap.error_type = err.classify_exception(exc)
            snap.data_source = "unavailable"
            self._cache_set(cache_id, snap.__dict__)
            return snap

        if hist.empty or "Close" not in hist.columns:
            snap.status = "empty"
            snap.error = likely_no_data_reason(ticker, provider_symbol)
            snap.error_type = err.classify_empty(
                "price", remapped=was_remapped(ticker, self.name)
            )
            self._cache_set(cache_id, snap.__dict__)
            return snap

        # Keep only the most recent lookback window.
        if "Date" in hist.columns:
            hist = hist.sort_values("Date")
        tail = hist.tail(max(lookback_days, 1))
        closes = pd.to_numeric(tail["Close"], errors="coerce").dropna()
        volumes = pd.to_numeric(tail.get("Volume", pd.Series(dtype=float)), errors="coerce").fillna(0)

        if not closes.empty:
            snap.last_close = coerce_float(closes.iloc[-1])
            if len(closes) > 1:
                first = closes.iloc[0]
                if first and first > 0:
                    snap.return_pct = float((closes.iloc[-1] / first) - 1.0)
                daily_ret = closes.pct_change().dropna()
                if len(daily_ret) >= 5:
                    stdev = float(daily_ret.std())
                    if math.isfinite(stdev):
                        snap.volatility_pct = stdev * math.sqrt(252)

        if not volumes.empty:
            avg_vol = float(volumes.tail(20).mean()) if len(volumes) >= 20 else float(volumes.mean())
            snap.avg_daily_volume = avg_vol
            if snap.last_close is not None:
                snap.avg_dollar_volume = avg_vol * snap.last_close

        for fld in ("last_close", "avg_daily_volume", "avg_dollar_volume",
                    "return_pct", "volatility_pct"):
            v = getattr(snap, fld)
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                setattr(snap, fld, None)

        self._cache_set(cache_id, snap.__dict__)
        return snap
