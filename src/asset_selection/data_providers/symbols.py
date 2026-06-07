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
from typing import Any, Dict, List, Mapping, Optional

# Providers we know how to translate for. Unknown providers fall through to a
# no-op (return the canonical symbol unchanged), which is the safe default.
# Both yfinance and Stooq spell US class shares with a hyphen (BRK-B); Stooq
# additionally needs a ``.us`` market suffix, applied by the Stooq provider.
_DOT_HYPHEN_PROVIDERS = {"yfinance", "stooq"}

# Provider families that resolve like Yahoo / yfinance (US symbols verbatim,
# class shares with a hyphen). Kept as a set so future Yahoo-shaped providers
# can opt in without special-casing.
_YAHOO_LIKE = {"yfinance", "yahoo"}

# A canonical class-share / suffixed common stock looks like ROOT '.' SUFFIX,
# e.g. 'BRK.B', 'BF.A', 'AKO.B'. We only remap the dot form; anything else is
# returned unchanged.
_CLASS_SHARE_RE = re.compile(r"^[A-Z0-9]+\.[A-Z]{1,3}$")

# --- Alias maps for known difficult / important symbols ---------------------
# These are *explicit* overrides applied first by the resolution ladder. The
# generic dot->hyphen rule already covers BRK.B/BF.B; the alias map exists so a
# truly idiosyncratic spelling (or a name we want to pin) can be corrected
# without widening the generic rule. GOOG and GOOGL are intentionally NOT
# aliased to each other -- they are distinct share classes.
_YFINANCE_ALIASES: Dict[str, str] = {
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
    "BF.B": "BF-B",
    "BF.A": "BF-A",
}
_STOOQ_ALIASES: Dict[str, str] = {
    # Stooq US symbols are lower-case with a ``.us`` market suffix; class shares
    # use a hyphen. These pin the few names worth being explicit about.
    "BRK.B": "brk-b.us",
    "BF.B": "bf-b.us",
}


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


def stooq_symbol(canonical: str) -> str:
    """Canonical -> primary Stooq symbol, e.g. AAPL -> aapl.us, BRK.B -> brk-b.us.

    Centralised here (rather than only in the Stooq provider) so the resolution
    ladder and the diagnostics speak the same Stooq spelling.
    """
    base = to_provider_symbol((canonical or "").strip().upper(), "stooq").lower()
    return f"{base}.us"


def resolve_provider_symbols(
    canonical: str,
    provider: str = "yfinance",
    metadata: Optional[Mapping[str, Any]] = None,
    *,
    extended: bool = True,
) -> List[str]:
    """Ordered, de-duplicated list of provider symbols to try for ``canonical``.

    The *first* element is always the same value :func:`to_provider_symbol` /
    :func:`stooq_symbol` would pick, so single-variant names (the overwhelming
    majority, e.g. ``NVDA``) cost exactly one attempt and behave exactly as
    before. Additional variants only appear for class shares, aliased names, or
    when ``metadata`` hints at an alternative — they are tried by the provider
    *only after an empty (no-data) response*, never after a transport error
    (where a different spelling cannot help). This keeps the ladder bounded:
    extra network calls are a function of symbol shape, not universe size.

    Ladder, in order:
      A. Alias override (explicit map for difficult/important symbols).
      B. Provider-normalized primary (yfinance: ``NVDA`` / ``BRK-B``;
         Stooq: ``nvda.us`` / ``brk-b.us``).
      C. Class-share variants (the dotted original and a hyphenated form).
      D. Exchange-aware / metadata hints, when provided.

    ``metadata`` may carry ``{"exchange": ..., "provider_symbol": ...}``; an
    explicit ``provider_symbol`` hint is appended as a candidate.
    """
    canon = (canonical or "").strip().upper()
    if not canon:
        return []
    prov = (provider or "").strip().lower()
    meta = dict(metadata or {})
    variants: List[str] = []

    if prov == "stooq":
        # A. alias, then B. provider-normalized primary.
        alias = _STOOQ_ALIASES.get(canon)
        if alias:
            variants.append(alias)
        variants.append(stooq_symbol(canon))
        if extended:
            # C. class-share / alternative spellings Stooq sometimes lists under.
            if "." in canon or "-" in canon:
                variants.append(f"{canon.lower()}.us")          # dotted form
                root = re.split(r"[.\-]", canon)[0].lower()
                if root:
                    variants.append(f"{root}.us")               # root-only last resort
    else:
        # Yahoo-like (default). Stooq-style ``.us`` spellings must NEVER appear
        # here -- that is the bug this whole milestone exists to prevent.
        alias = _YFINANCE_ALIASES.get(canon)
        if alias:
            variants.append(alias)
        variants.append(to_provider_symbol(canon, provider))     # B. primary
        if extended and is_class_share(canon):
            # C. class-share alternates: hyphen form (primary) + dotted original.
            variants.append(canon.replace(".", "-"))
            variants.append(canon)

    # D. explicit per-provider hint from metadata, if any.
    hint = meta.get("provider_symbol")
    if isinstance(hint, str) and hint.strip():
        variants.append(hint.strip())

    # De-duplicate while preserving order.
    seen = set()
    out: List[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


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
