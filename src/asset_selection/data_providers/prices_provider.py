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

from ..utils.validation import coerce_float
from .base import PriceSnapshot, PricesProvider
from .symbols import likely_no_data_reason, to_provider_symbol

logger = logging.getLogger(__name__)


class YFinancePricesProvider(PricesProvider):
    name = "yfinance"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=False)
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
        provider_symbol = to_provider_symbol(ticker, self.name)
        # Cache by provider symbol+window so a symbol-mapping change invalidates
        # cleanly and two canonical spellings can't collide.
        cache_id = f"{provider_symbol}:{lookback_days}"
        cached = self._cache_get(cache_id)
        if cached is not None:
            cached.setdefault("provider_symbol", provider_symbol)
            return PriceSnapshot(**cached)

        snap = PriceSnapshot(
            ticker=ticker, lookback_days=lookback_days, source=self.name,
            provider_symbol=provider_symbol,
        )

        try:
            hist = self._download_history(provider_symbol, lookback_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "yfinance price fetch errored for %s (as %s): %s",
                ticker, provider_symbol, exc,
            )
            snap.status = "error"
            snap.error = f"{type(exc).__name__}: {exc}"
            self._cache_set(cache_id, snap.__dict__)
            return snap

        if hist.empty or "Close" not in hist.columns:
            # The call succeeded but returned nothing usable. This is NOT an
            # exception and NOT the same as a genuinely-illiquid name we later
            # drop on the liquidity filter -- we record it as "empty" with an
            # honest, non-committal reason.
            snap.status = "empty"
            snap.error = likely_no_data_reason(ticker, provider_symbol)
            self._cache_set(cache_id, snap.__dict__)
            return snap

        closes = hist["Close"].dropna()
        volumes = hist.get("Volume", pd.Series(dtype=float)).fillna(0)

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

        self._cache_set(cache_id, snap.__dict__)
        return snap


def snapshot_to_dict(s: PriceSnapshot) -> Dict[str, Any]:
    return {k: v for k, v in s.__dict__.items()}
