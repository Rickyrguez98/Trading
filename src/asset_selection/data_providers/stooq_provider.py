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
from dataclasses import fields as _dc_fields
from typing import Any, Dict

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from . import errors as err
from .base import PriceSnapshot, PricesProvider
from .prices_provider import _populate_price_metrics, _run_symbol_ladder
from .symbols import (
    likely_no_data_reason,
    resolve_provider_symbols,
    stooq_symbol as _stooq_symbol,
    was_remapped,
)

logger = logging.getLogger(__name__)

_STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
_PRICE_FIELDS = {f.name for f in _dc_fields(PriceSnapshot)}


def _compat(cached: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in cached.items() if k in _PRICE_FIELDS}


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
        # Stooq resolution ladder: primary ``nvda.us`` first, then (only on an
        # EMPTY response) dotted / root-only spellings Stooq sometimes lists
        # under. variants[0] is always what ``stooq_symbol`` would return, so
        # the common single-variant case costs exactly one request.
        variants = resolve_provider_symbols(ticker, self.name) or [_stooq_symbol(ticker)]
        primary_symbol = variants[0]
        cache_id = f"{primary_symbol}:{lookback_days}"

        cached = self._cache_get(cache_id)
        if cached is not None:
            cached.setdefault("provider_symbol", primary_symbol)
            snap = PriceSnapshot(**_compat(cached))
            snap.data_source = "fresh_cache"
            return snap

        snap = PriceSnapshot(
            ticker=ticker, lookback_days=lookback_days, source=self.name,
            provider_symbol=primary_symbol, data_source="live",
        )

        # The Stooq CSV endpoint ignores any lookback hint (it returns the full
        # daily history), so the downloader takes only the symbol; we adapt it
        # to the ladder's ``(symbol, lookback_days)`` signature with a wrapper.
        hist, used_symbol, transport_exc = _run_symbol_ladder(
            ticker, variants, self.name,
            lambda sym, _days: self._download_csv(sym), lookback_days, snap,
        )
        snap.provider_symbol = used_symbol

        if transport_exc is not None:
            logger.warning(
                "stooq price fetch errored for %s (as %s): %s",
                ticker, used_symbol, transport_exc,
            )
            snap.status = "error"
            snap.error = f"{type(transport_exc).__name__}: {transport_exc}"
            snap.error_type = err.classify_exception(transport_exc)
            snap.data_source = "unavailable"
            self._cache_set(cache_id, snap.__dict__)
            return snap

        if hist is None or hist.empty or "Close" not in hist.columns:
            snap.status = "empty"
            snap.error = likely_no_data_reason(ticker, used_symbol)
            snap.error_type = err.classify_empty(
                "price", remapped=was_remapped(ticker, self.name)
            )
            self._cache_set(cache_id, snap.__dict__)
            return snap

        # Keep only the most recent lookback window, then share the exact metric
        # derivation used by the yfinance provider so the two never drift.
        if "Date" in hist.columns:
            hist = hist.sort_values("Date")
        tail = hist.tail(max(lookback_days, 1))
        closes = pd.to_numeric(tail["Close"], errors="coerce").dropna()
        volumes = pd.to_numeric(tail.get("Volume", pd.Series(dtype=float)), errors="coerce").fillna(0)
        _populate_price_metrics(snap, closes, volumes)
        self._cache_set(cache_id, snap.__dict__)
        return snap
