"""Build a cleaned U.S. common-stock universe.

Tries each source in order, applies symbol/name filters, deduplicates, and
returns a tidy DataFrame ready to be written to ``data/processed/universe.csv``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

from .config import AppConfig, UniverseConfig
from .data_providers.base import DataProvider
from .data_providers.ticker_provider import (
    NasdaqTraderTickerProvider,
    SECCompanyTickersProvider,
    TickerRecord,
)
from .utils.cache import Cache
from .utils.rate_limiter import RateLimiter
from .utils.validation import is_valid_ticker

logger = logging.getLogger(__name__)


# Substring patterns inside the SECURITY NAME that signal non-common-stock.
_NAME_BLOCKLIST = {
    "etf": re.compile(r"\b(etf|exchange[- ]traded|trust|fund|index)\b", re.IGNORECASE),
    "warrant": re.compile(r"\b(warrant|warrants|wts)\b", re.IGNORECASE),
    "unit": re.compile(r"\b(unit|units)\b", re.IGNORECASE),
    "preferred": re.compile(r"\b(preferred|pref|depositary)\b", re.IGNORECASE),
    "rights": re.compile(r"\b(rights?)\b", re.IGNORECASE),
    "notes": re.compile(r"\b(notes?|debenture|bond)\b", re.IGNORECASE),
}

# Ticker suffix conventions used by NASDAQ:
#   '$' or '.PR' / '-P' -> preferreds; 'W' or '.WS' / '+' -> warrants;
#   'U' / '.U' -> units; 'R' / '.R' / '^' -> rights.
# We err on the side of *keeping* a symbol unless we have signal it's excluded.
_SUFFIX_PATTERNS = {
    "warrant": re.compile(r"(W$|\.WS|\+)"),
    "preferred": re.compile(r"(\.PR|\-P[A-Z]?$|\$)"),
    "unit": re.compile(r"(U$|\.U)"),
    "rights": re.compile(r"(R$|\.R|\^)"),
}


def build_universe(
    config: AppConfig,
    cache: Optional[Cache] = None,
    rate_limiter: Optional[RateLimiter] = None,
) -> pd.DataFrame:
    """Construct the cleaned universe DataFrame.

    Columns: ticker, company_name, exchange, asset_type, is_etf, is_test_issue,
    source.
    """
    cache = cache or Cache(directory=config.cache.dir, enabled=config.cache.enabled)
    rate_limiter = rate_limiter or RateLimiter(config.rate_limits.get("yfinance", 0.4))

    raw: List[TickerRecord] = []
    used_sources: List[str] = []
    for source_name in config.universe.sources:
        provider = _instantiate_source(source_name, cache=cache, rate_limiter=rate_limiter)
        if provider is None:
            logger.warning("Unknown universe source %r — skipping.", source_name)
            continue
        try:
            rows = provider.fetch_all()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - fallback by design
            logger.warning("Universe source %s failed: %s", source_name, exc)
            continue
        if not rows:
            logger.warning("Universe source %s returned 0 rows.", source_name)
            continue
        raw.extend(rows)
        used_sources.append(source_name)
        # If the primary source succeeded we don't need fallbacks.
        if len(rows) > 1000:
            break

    if not raw:
        raise RuntimeError(
            "All universe sources failed. Check network connectivity and "
            "rerun (or supply --tickers manually)."
        )

    df = _records_to_df(raw, sources=",".join(used_sources))
    cleaned = clean_universe(df, config.universe)
    logger.info(
        "Universe: %d raw -> %d cleaned (sources=%s).", len(df), len(cleaned), used_sources
    )
    return cleaned


def _instantiate_source(
    name: str, cache: Cache, rate_limiter: RateLimiter
) -> Optional[DataProvider]:
    if name == "nasdaq_trader":
        return NasdaqTraderTickerProvider(cache=cache, rate_limiter=rate_limiter)
    if name == "sec_company_tickers":
        return SECCompanyTickersProvider(cache=cache, rate_limiter=rate_limiter)
    return None


def _records_to_df(records: Iterable[TickerRecord], sources: str) -> pd.DataFrame:
    df = pd.DataFrame([r.__dict__ for r in records])
    if df.empty:
        return df
    df = df.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    df["source"] = sources
    return df


# ---------------------------------------------------------------------------
# Public cleaning helper (also imported by tests).
# ---------------------------------------------------------------------------

def clean_universe(df: pd.DataFrame, cfg: UniverseConfig) -> pd.DataFrame:
    """Apply name/suffix/flag filters and ticker validation."""
    if df.empty:
        return df

    work = df.copy()

    # Normalize.
    work["ticker"] = work["ticker"].astype(str).str.strip().str.upper()
    if "company_name" in work.columns:
        work["company_name"] = work["company_name"].astype(str).str.strip()

    keep = pd.Series(True, index=work.index)

    # Ticker symbol sanity.
    keep &= work["ticker"].apply(
        lambda t: is_valid_ticker(t, cfg.min_ticker_length, cfg.max_ticker_length)
    )

    # Test issues.
    if cfg.exclude_test_issues and "is_test_issue" in work.columns:
        keep &= ~work["is_test_issue"].fillna(False).astype(bool)

    # Provider ETF flag.
    if cfg.exclude_etfs and "is_etf" in work.columns:
        keep &= ~work["is_etf"].fillna(False).astype(bool)

    # Name-based filters.
    name_series = work.get("company_name", pd.Series("", index=work.index)).fillna("")

    if cfg.exclude_etfs:
        keep &= ~name_series.str.contains(_NAME_BLOCKLIST["etf"])
    if cfg.exclude_warrants:
        keep &= ~name_series.str.contains(_NAME_BLOCKLIST["warrant"])
        keep &= ~work["ticker"].str.contains(_SUFFIX_PATTERNS["warrant"])
    if cfg.exclude_units:
        keep &= ~name_series.str.contains(_NAME_BLOCKLIST["unit"])
        keep &= ~work["ticker"].str.contains(_SUFFIX_PATTERNS["unit"])
    if cfg.exclude_preferred:
        keep &= ~name_series.str.contains(_NAME_BLOCKLIST["preferred"])
        keep &= ~work["ticker"].str.contains(_SUFFIX_PATTERNS["preferred"])
    if cfg.exclude_rights:
        keep &= ~name_series.str.contains(_NAME_BLOCKLIST["rights"])
        keep &= ~work["ticker"].str.contains(_SUFFIX_PATTERNS["rights"])

    # Always exclude obvious debt / notes.
    keep &= ~name_series.str.contains(_NAME_BLOCKLIST["notes"])

    cleaned = work.loc[keep].copy()
    cleaned["asset_type"] = cleaned.get("asset_type", "common").fillna("common")
    return cleaned.reset_index(drop=True)


def save_universe(df: pd.DataFrame, path: str) -> Path:
    """Persist to CSV; create parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    logger.info("Saved universe -> %s (%d rows).", p, len(df))
    return p
