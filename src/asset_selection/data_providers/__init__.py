"""Pluggable data providers.

All providers inherit from :class:`base.DataProvider` and are looked up by
short name (e.g. ``"yfinance"``) via the registry helpers below.
"""
from __future__ import annotations

from typing import Type

from .base import DataProvider, FundamentalsProvider, NewsProvider, PricesProvider
from .fundamentals_provider import YFinanceFundamentalsProvider
from .news_provider import YFinanceNewsProvider
from .prices_provider import YFinancePricesProvider
from .ticker_provider import NasdaqTraderTickerProvider, SECCompanyTickersProvider

_FUNDAMENTALS_REGISTRY: dict[str, Type[FundamentalsProvider]] = {
    "yfinance": YFinanceFundamentalsProvider,
}

_PRICES_REGISTRY: dict[str, Type[PricesProvider]] = {
    "yfinance": YFinancePricesProvider,
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


__all__ = [
    "DataProvider",
    "FundamentalsProvider",
    "NewsProvider",
    "PricesProvider",
    "YFinanceFundamentalsProvider",
    "YFinanceNewsProvider",
    "YFinancePricesProvider",
    "NasdaqTraderTickerProvider",
    "SECCompanyTickersProvider",
    "get_fundamentals_provider",
    "get_prices_provider",
    "get_news_provider",
]
