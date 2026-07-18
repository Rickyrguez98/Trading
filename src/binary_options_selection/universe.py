"""Underlying-instrument universe for binary-options analysis.

Binary-options platforms list only a subset of instruments, so this module
never assumes "every U.S. stock is available". Sources, all replaceable:

1. ``custom``          — an explicit ticker list (CLI ``--tickers`` or YAML).
2. ``watchlist``       — the configured watchlist (YAML list or a text file,
                          one symbol per line, ``#`` comments allowed). The
                          safest default.
3. ``asset-selection`` — the equity pipeline's output CSV, used ONLY as an
                          upstream universe/liquidity/quality filter. A strong
                          long-horizon fundamental score is never treated as a
                          predictor of 5-minute direction. When present, the
                          CSV's ``sentiment_score`` is carried along as the
                          optional (capped, extended-horizon-only) sentiment
                          feature input.

A future broker-specific availability provider can implement this same
resolve() contract without touching the pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import BinaryOptionsConfig

logger = logging.getLogger(__name__)


@dataclass
class UniverseResult:
    mode: str
    symbols: List[str] = field(default_factory=list)
    # Optional per-symbol context carried from the asset-selection CSV.
    sentiment_by_symbol: Dict[str, float] = field(default_factory=dict)
    name_by_symbol: Dict[str, str] = field(default_factory=dict)
    dropped: Dict[str, str] = field(default_factory=dict)   # symbol -> reason
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None


def _clean_symbols(raw: List[str], max_symbols: int, dropped: Dict[str, str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for s in raw:
        sym = str(s).strip().upper()
        if not sym or sym.startswith("#"):
            continue
        if not all(ch.isalnum() or ch in ".-" for ch in sym) or len(sym) > 10:
            dropped[sym] = "invalid_symbol"
            continue
        if sym in seen:
            dropped[sym] = "duplicate"
            continue
        seen.add(sym)
        out.append(sym)
    if len(out) > max_symbols:
        for sym in out[max_symbols:]:
            dropped[sym] = "over_max_symbols"
        out = out[:max_symbols]
    return out


def resolve_universe(
    cfg: BinaryOptionsConfig,
    *,
    cli_tickers: Optional[List[str]] = None,
    cli_watchlist_file: Optional[str] = None,
) -> UniverseResult:
    mode = cfg.universe.mode
    dropped: Dict[str, str] = {}
    result = UniverseResult(mode=mode)

    if mode == "catalog":
        result.error = (
            "universe.mode 'catalog' is resolved by the pipeline from the "
            "instrument catalog; resolve_universe only handles research modes"
        )
        return result

    if mode == "custom":
        raw = list(cli_tickers or cfg.universe.tickers)
        if not raw:
            result.error = "universe mode 'custom' but no tickers were provided"
            return result
        result.symbols = _clean_symbols(raw, cfg.universe.max_symbols, dropped)

    elif mode == "watchlist":
        raw = list(cfg.universe.watchlist)
        path = cli_watchlist_file or cfg.universe.watchlist_file
        if path:
            p = Path(path)
            if not p.exists():
                result.error = f"watchlist file not found: {path}"
                return result
            raw = [line for line in p.read_text(encoding="utf-8").splitlines()]
            result.notes.append(f"watchlist loaded from {path}")
        if not raw:
            result.error = "universe mode 'watchlist' but the watchlist is empty"
            return result
        result.symbols = _clean_symbols(raw, cfg.universe.max_symbols, dropped)

    elif mode == "asset-selection":
        result = _from_asset_selection(cfg, dropped)

    else:  # config validation should prevent this; keep the honest fallback
        result.error = f"unknown universe mode {mode!r}"
        return result

    result.dropped = dropped
    if not result.error and not result.symbols:
        result.error = "universe resolved to zero symbols"
    return result


def _from_asset_selection(
    cfg: BinaryOptionsConfig, dropped: Dict[str, str]
) -> UniverseResult:
    import pandas as pd

    result = UniverseResult(mode="asset-selection")
    path = Path(cfg.universe.asset_selection_input)
    if not path.exists():
        result.error = (
            f"asset-selection input not found: {path} -- run the equity pipeline "
            "first or switch universe.mode to custom/watchlist"
        )
        return result
    try:
        df = pd.read_csv(path)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        result.error = f"could not read {path}: {type(exc).__name__}: {exc}"
        return result
    if "ticker" not in df.columns:
        result.error = f"{path} has no 'ticker' column; not an asset-selection output"
        return result

    # The CSV is already ranked; keep the configured top-K as the upstream
    # quality/liquidity filter (NOT as a directional signal).
    top_k = cfg.universe.asset_selection_top_k
    if top_k:
        df = df.head(int(top_k))
    raw = [str(t) for t in df["ticker"].tolist()]
    result.symbols = _clean_symbols(raw, cfg.universe.max_symbols, dropped)
    result.notes.append(
        f"asset-selection input: {path} (top_k={top_k}); used as an upstream "
        "universe filter only -- fundamentals are not an intraday predictor"
    )
    kept = set(result.symbols)
    if "sentiment_score" in df.columns:
        for _, row in df.iterrows():
            sym = str(row["ticker"]).strip().upper()
            if sym in kept and row.get("sentiment_score") == row.get("sentiment_score"):
                result.sentiment_by_symbol[sym] = float(row["sentiment_score"])
    if "company_name" in df.columns:
        for _, row in df.iterrows():
            sym = str(row["ticker"]).strip().upper()
            if sym in kept and isinstance(row.get("company_name"), str):
                result.name_by_symbol[sym] = row["company_name"]
    return result
