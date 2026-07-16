"""Shared, domain-free infrastructure used by every research module.

This package holds only genuinely generic components — on-disk caching,
request pacing, filesystem helpers, and the transport-level provider error
taxonomy. Domain-specific logic (equity fundamentals, intraday candles, …)
stays in the respective module packages (``asset_selection``,
``binary_options_selection``).
"""
from .cache import Cache, CacheEntry  # noqa: F401
from .rate_limiter import RateLimiter  # noqa: F401
