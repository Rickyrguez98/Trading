"""Fundamentals via yfinance (default, free, keyless).

Every numeric field is best-effort: yfinance regularly returns ``None`` or
``nan`` for fields that an issuer simply does not report. We translate those
to ``None`` (true missing) so the scorer can penalize them rather than treat
zero as a real datapoint.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from tenacity import retry, stop_after_attempt, wait_exponential

from dataclasses import fields as _dc_fields

from ..utils.validation import coerce_float
from . import errors as err
from .base import Fundamentals, FundamentalsProvider
from .symbols import likely_no_data_reason, to_provider_symbol, was_remapped

logger = logging.getLogger(__name__)

_FUND_FIELDS = {f.name for f in _dc_fields(Fundamentals)}


def _compat(cached: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only keys the current ``Fundamentals`` schema knows about."""
    return {k: v for k, v in cached.items() if k in _FUND_FIELDS}


# Mapping from yfinance ``info`` keys to our schema. Listed only the keys that
# we care about; any other yfinance field is ignored.
_INFO_MAP: Dict[str, str] = {
    "longName": "company_name",
    "shortName": "company_name",
    "sector": "sector",
    "industry": "industry",
    "exchange": "exchange",
    "marketCap": "market_cap",
    "sharesOutstanding": "shares_outstanding",
    "revenueGrowth": "revenue_growth",
    "earningsGrowth": "earnings_growth",
    "earningsQuarterlyGrowth": "earnings_growth",  # fallback
    "operatingMargins": "operating_margin",
    "profitMargins": "net_margin",
    "returnOnEquity": "roe",
    "returnOnAssets": "roa",
    "debtToEquity": "debt_to_equity",
    "currentRatio": "current_ratio",
    "freeCashflow": "free_cash_flow",
    "operatingCashflow": "operating_cash_flow",
    "trailingPE": "pe_ratio",
    "forwardPE": "forward_pe",
    "pegRatio": "peg_ratio",
    "priceToSalesTrailing12Months": "price_to_sales",
    "priceToBook": "price_to_book",
}

# Fields we expect to be populated for a "complete" record. If any are None
# after extraction they go on the missing list.
_TRACKED_FIELDS: List[str] = [
    "market_cap",
    "revenue_growth",
    "earnings_growth",
    "operating_margin",
    "net_margin",
    "roe",
    "roa",
    "debt_to_equity",
    "current_ratio",
    "free_cash_flow",
    "operating_cash_flow",
    "pe_ratio",
    "forward_pe",
    "peg_ratio",
    "price_to_sales",
    "price_to_book",
]


class YFinanceFundamentalsProvider(FundamentalsProvider):
    name = "yfinance"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _fetch_info(self, provider_symbol: str) -> Dict[str, Any]:
        import yfinance as yf

        self.rate_limiter.acquire()
        tk = yf.Ticker(provider_symbol)
        # yfinance 0.2.40+ exposes .get_info(); older versions only have .info.
        try:
            info = tk.get_info()  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            info = tk.info  # type: ignore[attr-defined]
        return dict(info) if info else {}

    def fetch(self, ticker: str) -> Fundamentals:
        ticker = ticker.strip().upper()
        provider_symbol = to_provider_symbol(ticker, self.name)

        cache_id = provider_symbol
        cached = self._cache_get(cache_id)
        if cached is not None:
            cached.setdefault("provider_symbol", provider_symbol)
            out = Fundamentals(**_compat(cached))
            out.data_source = "fresh_cache"
            return out

        out = Fundamentals(
            ticker=ticker, source=self.name, as_of=self._now_iso(),
            provider_symbol=provider_symbol, data_source="live",
        )

        try:
            info = self._fetch_info(provider_symbol)
        except Exception as exc:  # noqa: BLE001 - report-and-continue semantics
            logger.warning(
                "yfinance info fetch errored for %s (as %s): %s",
                ticker, provider_symbol, exc,
            )
            info = {}
            out.status = "error"
            out.error = f"{type(exc).__name__}: {exc}"
            out.error_type = err.classify_exception(exc)
            out.data_source = "unavailable"

        if not info and out.status != "error":
            # Successful call, empty payload: honest "empty", not "delisted".
            out.status = "empty"
            out.error = likely_no_data_reason(ticker, provider_symbol)
            out.error_type = err.classify_empty(
                "fundamentals", remapped=was_remapped(ticker, self.name)
            )

        # Identity / metadata fields (strings; left as-is or None).
        for src, dst in _INFO_MAP.items():
            if src not in info or info[src] in (None, ""):
                continue
            cur = getattr(out, dst, None)
            if cur is not None:
                continue  # already set by a higher-priority alias
            value = info[src]
            if dst in {"company_name", "sector", "industry", "exchange"}:
                setattr(out, dst, str(value))
            else:
                setattr(out, dst, coerce_float(value))

        # Derived: FCF yield and OCF margin (need market cap / revenue).
        revenue = coerce_float(info.get("totalRevenue"))
        if out.free_cash_flow is not None and out.market_cap and out.market_cap > 0:
            out.free_cash_flow_yield = out.free_cash_flow / out.market_cap
        if out.operating_cash_flow is not None and revenue and revenue > 0:
            out.operating_cash_flow_margin = out.operating_cash_flow / revenue

        # Net income growth: yfinance doesn't expose this directly. We
        # *approximate* it with earningsGrowth (already mapped to
        # earnings_growth) and leave net_income_growth blank rather than
        # double-assigning the same number to two fields.

        out.missing_fields = [f for f in _TRACKED_FIELDS if getattr(out, f) is None]

        self._cache_set(ticker, out.__dict__)
        return out
