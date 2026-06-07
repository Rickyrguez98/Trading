"""Critical / material ticker resolution.

A *critical* ticker is one whose silent disappearance from a ranking is a
data-quality red flag, not an economic signal: a configured mega-cap, a
benchmark bellwether, or a name the user explicitly watches. The price funnel
uses this to decide where to spend extra effort (the full symbol ladder plus a
cross-provider fundamentals confirmation) before classifying a price miss, and
the validation layer uses it to report material gaps loudly instead of letting
99.8% headline coverage bury a missing NVDA.

Two flavours of criticality:

* **static** -- known ahead of time from config (static set, user watchlist,
  benchmark bellwethers). Available at Stage 2, before any data is fetched.
* **dynamic** -- only knowable from fetched data (very high dollar volume from
  the price snapshot; very large market cap from fundamentals). Applied where
  the relevant metric exists.

This module is pure and dependency-light so it is trivially testable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Optional, Set

from .health import BENCHMARK_TICKERS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import CriticalTickersConfig


def _norm(ticker: Optional[str]) -> str:
    return (ticker or "").strip().upper()


def resolve_static_critical_set(cfg: "CriticalTickersConfig") -> Set[str]:
    """Union of the always-critical tickers known before any fetch.

    Combines the configured static set, the user watchlist, and (when
    ``treat_benchmark_as_critical``) the health-check bellwethers. Canonical
    upper-case spellings; never mutated.
    """
    out: Set[str] = set()
    for t in (cfg.static_tickers or []):
        n = _norm(t)
        if n:
            out.add(n)
    for t in (cfg.user_watchlist or []):
        n = _norm(t)
        if n:
            out.add(n)
    if getattr(cfg, "treat_benchmark_as_critical", False):
        for t in BENCHMARK_TICKERS:
            out.add(_norm(t))
    return out


def user_watchlist_set(cfg: "CriticalTickersConfig") -> Set[str]:
    return {_norm(t) for t in (cfg.user_watchlist or []) if _norm(t)}


def is_static_critical(ticker: str, critical_set: Iterable[str]) -> bool:
    return _norm(ticker) in {_norm(t) for t in critical_set}


def is_high_dollar_volume(
    avg_dollar_volume: Optional[float], cfg: "CriticalTickersConfig"
) -> bool:
    """True if a price snapshot's dollar volume marks the name as high-liquidity.

    Used as a dynamic-criticality signal: even a name that is not in the static
    set is material if it clearly trades in size. ``None`` (no data) is not
    high-liquidity.
    """
    try:
        adv = float(avg_dollar_volume)
    except (TypeError, ValueError):
        return False
    if adv != adv:  # NaN
        return False
    return adv >= float(getattr(cfg, "high_dollar_volume_for_critical", 0.0) or 0.0)


def is_large_cap(market_cap: Optional[float], cfg: "CriticalTickersConfig") -> bool:
    """True if a market cap marks the name as a (dynamic) large-cap critical."""
    try:
        mc = float(market_cap)
    except (TypeError, ValueError):
        return False
    if mc != mc:  # NaN
        return False
    return mc >= float(getattr(cfg, "large_cap_for_critical", 0.0) or 0.0)
