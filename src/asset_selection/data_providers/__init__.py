"""Pluggable data providers.

All providers inherit from :class:`base.DataProvider` and are looked up by
short name (e.g. ``"yfinance"``) via the registry helpers below.
"""
from __future__ import annotations

from typing import List, Optional, Type

from .base import DataProvider, FundamentalsProvider, NewsProvider, PricesProvider
from .fallback import (
    FallbackFundamentalsProvider,
    FallbackNewsProvider,
    FallbackPricesProvider,
)
from .fundamentals_provider import YFinanceFundamentalsProvider
from .news_provider import YFinanceNewsProvider
from .prices_provider import YFinancePricesProvider
from .stooq_provider import StooqPricesProvider
from .ticker_provider import NasdaqTraderTickerProvider, SECCompanyTickersProvider

_FUNDAMENTALS_REGISTRY: dict[str, Type[FundamentalsProvider]] = {
    "yfinance": YFinanceFundamentalsProvider,
}

_PRICES_REGISTRY: dict[str, Type[PricesProvider]] = {
    "yfinance": YFinancePricesProvider,
    "stooq": StooqPricesProvider,
}

_NEWS_REGISTRY: dict[str, Type[NewsProvider]] = {
    "yfinance": YFinanceNewsProvider,
}


def get_fundamentals_provider(name: str) -> Type[FundamentalsProvider]:
    try:
        return _FUNDAMENTALS_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown fundamentals provider: {name!r}. "
            f"Available: {sorted(_FUNDAMENTALS_REGISTRY)}"
        ) from exc


def get_prices_provider(name: str) -> Type[PricesProvider]:
    try:
        return _PRICES_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown prices provider: {name!r}. "
            f"Available: {sorted(_PRICES_REGISTRY)}"
        ) from exc


def get_news_provider(name: str) -> Type[NewsProvider]:
    try:
        return _NEWS_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown news provider: {name!r}. "
            f"Available: {sorted(_NEWS_REGISTRY)}"
        ) from exc


# ---------------------------------------------------------------------------
# Provider-chain builders (priority order + cache backup)
# ---------------------------------------------------------------------------

def _priority(config, data_type: str, default_name: str) -> List[str]:
    """Resolve the ordered provider names for ``data_type`` from config."""
    pri = (config.robustness.provider_priority_by_data_type or {}).get(data_type)
    if pri:
        return [str(n) for n in pri]
    return [default_name]


def _maybe_wrap(instances, wrapper_cls, config):
    """Return the lone provider unchanged, or wrap a chain / cache-backup run."""
    use_cache = bool(config.robustness.use_cache_on_provider_failure)
    if len(instances) == 1 and not use_cache:
        return instances[0]
    return wrapper_cls(
        instances,
        use_cache_on_failure=use_cache,
        max_cache_age_seconds=config.robustness.max_cache_age_seconds,
    )


def build_prices_provider(config, make_cache, make_rate_limiter):
    """Build the (possibly chained) prices provider from config priority.

    ``make_cache(namespace)`` returns a namespaced Cache; ``make_rate_limiter(name)``
    returns a RateLimiter for a provider name. Kept as callbacks so this module
    needs no knowledge of how the orchestrator wires caching/pacing.
    """
    names = _priority(config, "prices", config.providers.prices)
    instances = [
        get_prices_provider(n)(cache=make_cache("prices"), rate_limiter=make_rate_limiter(n))
        for n in names
    ]
    return _maybe_wrap(instances, FallbackPricesProvider, config)


def build_fundamentals_provider(config, make_cache, make_rate_limiter):
    names = _priority(config, "fundamentals", config.providers.fundamentals)
    instances = [
        get_fundamentals_provider(n)(cache=make_cache("fundamentals"), rate_limiter=make_rate_limiter(n))
        for n in names
    ]
    return _maybe_wrap(instances, FallbackFundamentalsProvider, config)


def build_news_provider(config, make_cache, make_rate_limiter):
    names = _priority(config, "news", config.providers.news)
    instances = [
        get_news_provider(n)(cache=make_cache("news"), rate_limiter=make_rate_limiter(n))
        for n in names
    ]
    return _maybe_wrap(instances, FallbackNewsProvider, config)


__all__ = [
    "DataProvider",
    "FundamentalsProvider",
    "NewsProvider",
    "PricesProvider",
    "YFinanceFundamentalsProvider",
    "YFinanceNewsProvider",
    "YFinancePricesProvider",
    "StooqPricesProvider",
    "NasdaqTraderTickerProvider",
    "SECCompanyTickersProvider",
    "FallbackFundamentalsProvider",
    "FallbackNewsProvider",
    "FallbackPricesProvider",
    "get_fundamentals_provider",
    "get_prices_provider",
    "get_news_provider",
    "build_fundamentals_provider",
    "build_prices_provider",
    "build_news_provider",
]
