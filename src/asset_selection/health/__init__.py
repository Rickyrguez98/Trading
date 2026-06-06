"""Provider health checks.

Before committing to a full pipeline run, probe each data provider against a
handful of benchmark mega-caps. If the most-liquid names on earth (AAPL, MSFT,
GOOGL) cannot be priced, the problem is the **provider** (blocked/rate-limited),
not the tickers -- and we should refuse to present a normal ranking.
"""
from .provider_health import (
    BENCHMARK_TICKERS,
    HealthCheckResult,
    run_provider_health_checks,
    summarize_health,
)

__all__ = [
    "BENCHMARK_TICKERS",
    "HealthCheckResult",
    "run_provider_health_checks",
    "summarize_health",
]
