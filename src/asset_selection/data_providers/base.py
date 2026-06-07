"""Common interfaces for all data providers.

Every concrete provider receives a ``Cache`` and a ``RateLimiter`` at
construction time so the pipeline can wire them with the right config without
the provider having to know how caching or pacing work.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..utils.cache import Cache
from ..utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Records (provider-agnostic schemas)
# ---------------------------------------------------------------------------

@dataclass
class Fundamentals:
    """Per-ticker fundamentals snapshot.

    All numeric fields are ``Optional[float]`` — None means the provider did
    not return that field. The scoring layer treats None as missing data and
    penalizes it, rather than silently imputing zero.
    """
    ticker: str

    # Identity
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    exchange: Optional[str] = None

    # Size & market
    market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None

    # Growth
    revenue_growth: Optional[float] = None
    earnings_growth: Optional[float] = None
    net_income_growth: Optional[float] = None
    fcf_growth: Optional[float] = None

    # Profitability
    operating_margin: Optional[float] = None
    net_margin: Optional[float] = None
    roe: Optional[float] = None
    roa: Optional[float] = None

    # Balance sheet
    debt_to_equity: Optional[float] = None
    current_ratio: Optional[float] = None

    # Cash flow
    free_cash_flow: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    free_cash_flow_yield: Optional[float] = None
    operating_cash_flow_margin: Optional[float] = None

    # Valuation
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    peg_ratio: Optional[float] = None
    price_to_sales: Optional[float] = None
    price_to_book: Optional[float] = None

    # Metadata
    as_of: Optional[str] = None  # ISO timestamp from provider, if available
    source: Optional[str] = None
    missing_fields: List[str] = field(default_factory=list)
    # Provenance / honesty fields (see PriceSnapshot for semantics).
    provider_symbol: Optional[str] = None
    status: str = "ok"
    error: Optional[str] = None
    # Machine-readable error taxonomy (see data_providers/errors.py). None on a
    # clean record; set to a constant like PROVIDER_JSON_PARSE_ERROR otherwise.
    error_type: Optional[str] = None
    # Where the data came from: "live" | "fresh_cache" | "stale_cache" |
    # "fallback" | "unavailable". Lets reports state provenance honestly.
    data_source: Optional[str] = None


@dataclass
class PriceSnapshot:
    ticker: str                               # canonical symbol (e.g. BRK.B)
    last_close: Optional[float] = None
    avg_daily_volume: Optional[float] = None  # shares
    avg_dollar_volume: Optional[float] = None
    return_pct: Optional[float] = None        # over lookback window
    volatility_pct: Optional[float] = None    # annualized stdev of daily returns
    lookback_days: Optional[int] = None
    source: Optional[str] = None
    # Provenance / honesty fields. ``provider_symbol`` is what we actually sent
    # to the provider (e.g. BRK-B). ``status`` is one of "ok" | "empty" |
    # "error"; "empty" means the call succeeded but returned no usable data
    # (NOT the same as a genuinely illiquid name that the filter later drops).
    provider_symbol: Optional[str] = None
    status: str = "ok"
    error: Optional[str] = None
    # Machine-readable error taxonomy (see data_providers/errors.py).
    error_type: Optional[str] = None
    # Provenance: "live" | "fresh_cache" | "stale_cache" | "fallback" | "unavailable".
    data_source: Optional[str] = None
    # Per-attempt trail across the symbol-resolution ladder AND the provider
    # chain. Each entry is a plain dict (JSON-friendly) with the audit fields:
    # canonical_symbol, provider_name, provider_symbol, symbol_variant_attempted,
    # success, error_type, error_message, response_summary. This is what lets the
    # diagnostics say *which* provider/symbol was tried instead of collapsing
    # everything into a single NO_PRICE_DATA. Empty on a clean first-try success.
    provider_attempts: List[Dict[str, Any]] = field(default_factory=list)


def make_provider_attempt(
    *,
    canonical_symbol: str,
    provider_name: str,
    provider_symbol: str,
    success: bool,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    response_summary: Optional[str] = None,
    symbol_variant_attempted: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one normalized provider-attempt record (the audit's required shape).

    ``symbol_variant_attempted`` defaults to ``provider_symbol`` (the spelling we
    actually sent) so callers that try a single variant need not pass it twice.
    """
    return {
        "canonical_symbol": canonical_symbol,
        "provider_name": provider_name,
        "provider_symbol": provider_symbol,
        "symbol_variant_attempted": symbol_variant_attempted or provider_symbol,
        "success": bool(success),
        "error_type": error_type,
        "error_message": error_message,
        "response_summary": response_summary,
    }


@dataclass
class NewsItem:
    ticker: str
    headline: str
    summary: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    published_at: Optional[str] = None        # ISO-8601 UTC
    retrieved_at: Optional[str] = None        # ISO-8601 UTC


# ---------------------------------------------------------------------------
# Abstract providers
# ---------------------------------------------------------------------------

class DataProvider(ABC):
    """Base class for every provider. Holds wiring; subclasses implement fetch."""

    name: str = "base"
    cache_namespace: str = "base"
    cache_ttl_seconds: int = 3600

    def __init__(
        self,
        cache: Optional[Cache] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ) -> None:
        self.cache = cache or Cache(enabled=False)
        self.rate_limiter = rate_limiter or RateLimiter(0.0)

    # ------------------------------------------------------------------
    def _cache_get(self, identifier: str) -> Optional[Any]:
        return self.cache.get(self.cache_namespace, identifier, ttl=self.cache_ttl_seconds)

    def _cache_set(self, identifier: str, payload: Any) -> None:
        self.cache.set(self.cache_namespace, identifier, payload)

    # ------------------------------------------------------------------
    @staticmethod
    def _now_iso() -> str:
        return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class FundamentalsProvider(DataProvider):
    cache_namespace = "fundamentals"

    @abstractmethod
    def fetch(self, ticker: str) -> Fundamentals:
        """Return a Fundamentals record; never raise on missing fields."""


class PricesProvider(DataProvider):
    cache_namespace = "prices"

    @abstractmethod
    def fetch(self, ticker: str, lookback_days: int = 90) -> PriceSnapshot:
        """Return a PriceSnapshot with liquidity metrics."""


class NewsProvider(DataProvider):
    cache_namespace = "news"

    @abstractmethod
    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        """Return a list of NewsItem. Empty list is a valid response."""


def fundamentals_to_dict(f: Fundamentals) -> Dict[str, Any]:
    """Flatten a Fundamentals dataclass to a dict (for DataFrame ingestion)."""
    return {k: v for k, v in f.__dict__.items()}
