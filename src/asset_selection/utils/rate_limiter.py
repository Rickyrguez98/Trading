"""Backward-compatible shim: the limiter now lives in ``trading_common.rate_limiter``."""
from trading_common.rate_limiter import RateLimiter  # noqa: F401
