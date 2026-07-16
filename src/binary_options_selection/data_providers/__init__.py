"""Intraday provider registry.

Register a new provider by subclassing :class:`IntradayCandlesProvider`,
adding it to ``_REGISTRY``, and listing its name in
``config.IMPLEMENTED_PROVIDERS`` (so config validation accepts it). A future
broker-specific availability provider would plug in here without any change
to the pipeline or scoring layers.
"""
from __future__ import annotations

from typing import Dict, Optional, Type

from trading_common.cache import Cache
from trading_common.rate_limiter import RateLimiter

from .base import Candle, IntradayCandlesProvider, IntradaySeries  # noqa: F401
from .yfinance_intraday import YFinanceIntradayProvider

_REGISTRY: Dict[str, Type[IntradayCandlesProvider]] = {
    "yfinance": YFinanceIntradayProvider,
}


def available_providers() -> tuple:
    return tuple(sorted(_REGISTRY))


def build_intraday_provider(
    name: str,
    cache: Optional[Cache] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> IntradayCandlesProvider:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown intraday provider {name!r}; available: {available_providers()}"
        ) from None
    return cls(cache=cache, rate_limiter=rate_limiter)
