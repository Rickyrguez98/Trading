"""Prices via yfinance for liquidity filters and basic context.

We do not use prices for portfolio construction yet — just average dollar
volume, recent return, and a volatility proxy.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict

import numpy as np
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from dataclasses import fields as _dc_fields

from ..utils.validation import coerce_float
from . import errors as err
from .base import PriceSnapshot, PricesProvider, make_provider_attempt
from .symbols import (
    likely_no_data_reason,
    resolve_provider_symbols,
    to_provider_symbol,
    was_remapped,
)

logger = logging.getLogger(__name__)

_PRICE_FIELDS = {f.name for f in _dc_fields(PriceSnapshot)}


def _compat(cached: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only keys the current ``PriceSnapshot`` schema knows about.

    Lets a cache entry written by an older OR newer build reconstruct cleanly:
    unknown keys are dropped, missing keys fall back to dataclass defaults.
    """
    return {k: v for k, v in cached.items() if k in _PRICE_FIELDS}


class YFinancePricesProvider(PricesProvider):
    name = "yfinance"

    def cache_identifier(self, ticker: str, lookback_days: int = 90) -> str:
        provider_symbol = to_provider_symbol(ticker.strip().upper(), self.name)
        return f"{provider_symbol}:{lookback_days}"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _download_history(self, provider_symbol: str, lookback_days: int) -> pd.DataFrame:
        import yfinance as yf

        self.rate_limiter.acquire()
        # period= accepts strings like '3mo'; we pass days->period mapping.
        period = self._period_for(lookback_days)
        df = yf.Ticker(provider_symbol).history(period=period, auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    @staticmethod
    def _period_for(lookback_days: int) -> str:
        if lookback_days <= 30:
            return "1mo"
        if lookback_days <= 90:
            return "3mo"
        if lookback_days <= 180:
            return "6mo"
        if lookback_days <= 365:
            return "1y"
        return "2y"

    def fetch(self, ticker: str, lookback_days: int = 90) -> PriceSnapshot:
        ticker = ticker.strip().upper()
        # The resolution ladder: try the provider-normalized primary first, then
        # (only on an EMPTY response) class-share / alias variants. For yfinance
        # the primary is the canonical symbol itself (NVDA->NVDA), never a
        # Stooq-style ``nvda.us``.
        variants = resolve_provider_symbols(ticker, self.name) or [ticker]
        primary_symbol = variants[0]
        # Cache by the PRIMARY provider symbol+window so a symbol-mapping change
        # invalidates cleanly and two canonical spellings can't collide.
        cache_id = f"{primary_symbol}:{lookback_days}"
        cached = self._cache_get(cache_id)
        if cached is not None:
            cached.setdefault("provider_symbol", primary_symbol)
            snap = PriceSnapshot(**_compat(cached))
            # A cache hit is within TTL by construction -> fresh cache.
            snap.data_source = "fresh_cache"
            return snap

        snap = PriceSnapshot(
            ticker=ticker, lookback_days=lookback_days, source=self.name,
            provider_symbol=primary_symbol, data_source="live",
        )
        hist, used_symbol, transport_exc = _run_symbol_ladder(
            ticker, variants, self.name, self._download_history, lookback_days, snap,
        )
        snap.provider_symbol = used_symbol

        if transport_exc is not None:
            logger.warning(
                "yfinance price fetch errored for %s (as %s): %s",
                ticker, used_symbol, transport_exc,
            )
            snap.status = "error"
            snap.error = f"{type(transport_exc).__name__}: {transport_exc}"
            snap.error_type = err.classify_exception(transport_exc)
            snap.data_source = "unavailable"
            self._cache_set(cache_id, snap.__dict__)
            return snap

        if hist is None or hist.empty or "Close" not in hist.columns:
            # Every variant returned an empty payload. This is NOT an exception
            # and NOT the same as a genuinely-illiquid name we later drop on the
            # liquidity filter -- we record it as "empty" with an honest,
            # non-committal reason and a NO_PRICE_DATA error type.
            snap.status = "empty"
            snap.error = likely_no_data_reason(ticker, used_symbol)
            snap.error_type = err.classify_empty(
                "price", remapped=was_remapped(ticker, self.name)
            )
            self._cache_set(cache_id, snap.__dict__)
            return snap

        closes = hist["Close"].dropna()
        volumes = hist.get("Volume", pd.Series(dtype=float)).fillna(0)
        _populate_price_metrics(snap, closes, volumes)
        self._cache_set(cache_id, snap.__dict__)
        return snap


def _run_symbol_ladder(canonical, variants, provider_name, downloader, lookback_days, snap):
    """Try each symbol variant in order, recording an attempt for each.

    Returns ``(hist_or_None, used_symbol, transport_exc_or_None)``:

      * On the first variant that returns a usable frame -> that frame + symbol.
      * On a transport/provider-side exception -> stop immediately (a different
        spelling cannot fix a blocked/rate-limited provider) and return the exc.
      * If every variant returns an empty payload -> ``(None, last_symbol, None)``.

    The per-variant attempts are appended to ``snap.provider_attempts`` so the
    diagnostics can show exactly what was tried.
    """
    used_symbol = variants[0] if variants else canonical
    for variant in variants:
        used_symbol = variant
        try:
            hist = downloader(variant, lookback_days)
        except Exception as exc:  # noqa: BLE001
            snap.provider_attempts.append(make_provider_attempt(
                canonical_symbol=canonical, provider_name=provider_name,
                provider_symbol=variant, success=False,
                error_type=err.classify_exception(exc),
                error_message=f"{type(exc).__name__}: {exc}",
                response_summary="exception during fetch",
            ))
            return None, used_symbol, exc
        if hist is not None and not getattr(hist, "empty", True) and "Close" in hist.columns:
            snap.provider_attempts.append(make_provider_attempt(
                canonical_symbol=canonical, provider_name=provider_name,
                provider_symbol=variant, success=True,
                response_summary=f"{len(hist)} row(s)",
            ))
            return hist, used_symbol, None
        # Empty payload -> record and try the next variant (symbol may be wrong).
        snap.provider_attempts.append(make_provider_attempt(
            canonical_symbol=canonical, provider_name=provider_name,
            provider_symbol=variant, success=False,
            error_type=err.classify_empty("price", remapped=(variant != canonical)),
            error_message="empty payload", response_summary="no rows",
        ))
    return None, used_symbol, None


def _populate_price_metrics(snap: PriceSnapshot, closes, volumes) -> None:
    """Fill last_close / return / volatility / volume metrics from OHLCV series.

    Shared by the yfinance and Stooq providers so the two never drift in how
    they derive liquidity/price-context metrics.
    """
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

    # Coerce NaNs to None so the rest of the system sees true missing.
    for fld in ("last_close", "avg_daily_volume", "avg_dollar_volume",
                "return_pct", "volatility_pct"):
        v = getattr(snap, fld)
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            setattr(snap, fld, None)


def snapshot_to_dict(s: PriceSnapshot) -> Dict[str, Any]:
    return {k: v for k, v in s.__dict__.items()}
