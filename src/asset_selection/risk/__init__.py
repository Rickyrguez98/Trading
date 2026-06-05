"""Risk management — **future milestone**.

Intentionally a stub. Today's pipeline only computes a coarse, per-ticker
``risk_penalty`` inside :mod:`asset_selection.scoring.composite_score`.

Planned, in this package:
    * per-name: realized vol, downside vol, max drawdown, beta vs SPY
    * portfolio: shrunk covariance, VaR / CVaR, sector + factor exposures
    * hard limits (concentration, sector caps) + soft alerts surfaced in reports
    * stress tests on canonical scenarios

A future ``portfolio_risk(weights, returns) -> RiskReport`` function will
live here.
"""

__all__: list[str] = []
