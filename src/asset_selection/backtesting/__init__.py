"""Backtesting — **future milestone**.

Intentionally a stub for now. The asset-selection pipeline does not import
from here.

Planned:
    * walk-forward replay of the selection rules
    * point-in-time fundamentals (avoid look-ahead bias)
    * rebalancing cadence (monthly/quarterly)
    * transaction cost + slippage models
    * Sharpe, Sortino, CAGR, max DD, turnover, hit rate

The backtester will consume the same per-ticker ``ScoreRow`` schema that the
selection pipeline produces today, so no re-engineering is required when this
milestone starts.
"""

__all__: list[str] = []
