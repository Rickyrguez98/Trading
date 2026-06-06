"""Per-provider ticker symbol normalization.

The *canonical* symbol is whatever the universe source (NASDAQ Trader) gives
us — e.g. class shares are dotted: ``BRK.B``, ``BF.B``. Different data
providers spell the same instrument differently:

- yfinance / Yahoo Finance use a hyphen for share classes: ``BRK-B``.
- Some vendors use a slash or a caret for preferreds, etc.

We keep the canonical symbol as the single key used everywhere in the
pipeline (universe, scoring, reports) and only translate to a
``provider_symbol`` at the boundary where we actually call the provider. We
never mutate the canonical ticker, so joins and reports stay stable.

This module is deliberately tiny and pure so it is trivially testable and has
no third-party dependencies.
"""
from __future__ import annotations

import re

# Providers we know how to translate for. Unknown providers fall through to a
# no-op (return the canonical symbol unchanged), which is the safe default.
_DOT_HYPHEN_PROVIDERS = {"yfinance"}

# A canonical class-share / suffixed common stock looks like ROOT '.' SUFFIX,
# e.g. 'BRK.B', 'BF.A', 'AKO.B'. We only remap the dot form; anything else is
# returned unchanged.
_CLASS_SHARE_RE = re.compile(r"^[A-Z0-9]+\.[A-Z]{1,3}$")


def to_provider_symbol(canonical: str, provider: str = "yfinance") -> str:
    """Translate a canonical symbol into the spelling ``provider`` expects.

    >>> to_provider_symbol("BRK.B", "yfinance")
    'BRK-B'
    >>> to_provider_symbol("AAPL", "yfinance")
    'AAPL'
    >>> to_provider_symbol("BRK.B", "finnhub")   # unknown -> unchanged
    'BRK.B'
    """
    s = (canonical or "").strip().upper()
    if not s:
        return s
    if provider in _DOT_HYPHEN_PROVIDERS and "." in s:
        # yfinance maps every dot to a hyphen for class shares; the cleaned
        # universe only ever contains dotted *class shares* (preferreds,
        # units, rights and warrants with dots are filtered upstream), so this
        # is safe.
        return s.replace(".", "-")
    return s


def was_remapped(canonical: str, provider: str = "yfinance") -> bool:
    """True if ``to_provider_symbol`` would change the symbol for ``provider``."""
    return to_provider_symbol(canonical, provider) != (canonical or "").strip().upper()


def is_class_share(canonical: str) -> bool:
    """True if the canonical symbol looks like a dotted class share (BRK.B)."""
    return bool(_CLASS_SHARE_RE.match((canonical or "").strip().upper()))


def likely_no_data_reason(canonical: str, provider_symbol: str) -> str:
    """Explain, honestly, why a provider returned no data for a symbol.

    We refuse to assert "delisted" as fact: after correct symbol
    normalization, an empty response can mean delisted, halted, recently
    listed, illiquid, or simply not covered by this free provider. The message
    lists the real possibilities instead of guessing one.
    """
    canon = (canonical or "").strip().upper()
    if canon != (provider_symbol or "").strip().upper():
        return (
            f"no data after normalizing {canon}->{provider_symbol}; symbol may "
            "be delisted, halted, recently listed, or not covered by the provider"
        )
    return (
        "no data returned; symbol may be illiquid, recently listed, delisted, "
        "halted, or not covered by the provider"
    )
