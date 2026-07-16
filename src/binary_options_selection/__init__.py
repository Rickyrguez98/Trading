"""Binary options selection — intraday directional research signals.

Selects candidate underlyings and a per-horizon directional research signal
(``CALL`` / ``PUT`` / ``NO_TRADE``) for configurable 5-120 minute expiration
horizons. Research output only: no execution, no broker connectivity, no
stake sizing, and heuristic scores are never presented as probabilities.
"""

__all__ = ["config"]
