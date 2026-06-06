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
    "etf": re.compile(r"\b(?:etf|exchange[- ]traded|trust|fund|index)\b", re.IGNORECASE),
    "warrant": re.compile(r"\b(?:warrant|warrants|wts)\b", re.IGNORECASE),
    "unit": re.compile(r"\b(?:unit|units)\b", re.IGNORECASE),
    "preferred": re.compile(r"\b(?:preferred|pref|depositary)\b", re.IGNORECASE),
    "rights": re.compile(r"\b(?:rights?)\b", re.IGNORECASE),
    "notes": re.compile(r"\b(?:notes?|debenture|bond)\b", re.IGNORECASE),
    # "When-Issued" / "When Issued" / trailing " WI", plus temporary lines.
    # These are conditional instruments with short, non-comparable history.
    "when_issued": re.compile(
        r"(?:\bwhen[\s-]?issued\b|\bwhen[\s-]?distributed\b|\bWI\b\s*$|\btemporary\b)",
        re.IGNORECASE,
    ),
}

# Ticker suffix conventions used by NASDAQ:
#   '$' or '.PR' / '-P' -> preferreds; 'W' or '.WS' / '+' -> warrants;
#   'U' / '.U' -> units; 'R' / '.R' / '^' -> rights.
# We err on the side of *keeping* a symbol unless we have signal it's excluded.
_SUFFIX_PATTERNS = {
    "warrant": re.compile(r"(?:W$|\.WS|\+)"),
    "preferred": re.compile(r"(?:\.PR|\-P[A-Z]?$|\$)"),
    "unit": re.compile(r"(?:U$|\.U)"),
    "rights": re.compile(r"(?:R$|\.R|\^)"),
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
    cleaned, clean_stats = clean_universe_with_stats(df, config.universe)
    # Stash removal-reason stats so stage 1 can report exactly what was removed
    # and why (When-Issued, ETF, preferred, ...). `.attrs` survives copy/head.
    cleaned.attrs["clean_stats"] = clean_stats
    if clean_stats.get("removed"):
        logger.info(
            "Universe cleaning removed %d rows: %s",
            clean_stats["raw"] - clean_stats["cleaned"],
            ", ".join(f"{k}={v}" for k, v in clean_stats["removed"].items()),
        )
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
    """Apply name/suffix/flag/exchange filters and ticker validation.

    Filtering is driven by the new ``include_*`` toggles on ``UniverseConfig``
    (with legacy ``exclude_*`` honoured via ``effective_include``). An empty
    ``exchanges`` list keeps every exchange in the input.

    This is the thin wrapper used by most callers; use
    :func:`clean_universe_with_stats` when you need the per-reason removal
    counts for reporting.
    """
    cleaned, _stats = clean_universe_with_stats(df, cfg)
    return cleaned


def clean_universe_with_stats(
    df: pd.DataFrame, cfg: UniverseConfig
) -> "tuple[pd.DataFrame, dict]":
    """Like :func:`clean_universe`, but also return removal-reason counts.

    Returns ``(cleaned_df, stats)`` where ``stats`` maps each removal reason
    (e.g. ``"when_issued"``, ``"etf_flag"``, ``"exchange_not_whitelisted"``) to
    the number of rows it removed. Each removed row is attributed to the first
    reason that caught it, so the counts sum to ``raw_count - cleaned_count``.
    """
    if df.empty:
        return df, {"raw": 0, "cleaned": 0, "removed": {}}

    work = df.copy()

    # Normalize.
    work["ticker"] = work["ticker"].astype(str).str.strip().str.upper()
    if "company_name" in work.columns:
        work["company_name"] = work["company_name"].astype(str).str.strip()

    name_series = work.get("company_name", pd.Series("", index=work.index)).fillna("")

    keep = pd.Series(True, index=work.index)
    removed: dict = {}

    def _drop(reason: str, mask: pd.Series) -> None:
        """Attribute rows caught by ``mask`` (and still kept) to ``reason``."""
        nonlocal keep
        mask = mask.reindex(work.index).fillna(False).astype(bool)
        newly = keep & mask
        count = int(newly.sum())
        if count:
            removed[reason] = removed.get(reason, 0) + count
        keep &= ~mask

    # Ticker symbol sanity.
    valid = work["ticker"].apply(
        lambda t: is_valid_ticker(t, cfg.min_ticker_length, cfg.max_ticker_length)
    )
    _drop("invalid_ticker", ~valid)

    # Exchange whitelist (if provided).
    if cfg.exchanges and "exchange" in work.columns:
        wanted = {_canon_exchange(x) for x in cfg.exchanges}
        in_wl = work["exchange"].astype(str).map(_canon_exchange).isin(wanted)
        _drop("exchange_not_whitelisted", ~in_wl)

    # Test issues.
    if not cfg.effective_include("test_issues") and "is_test_issue" in work.columns:
        _drop("test_issue", work["is_test_issue"].fillna(False).astype(bool))

    # Provider ETF flag + ETF/fund name.
    if not cfg.effective_include("etfs"):
        if "is_etf" in work.columns:
            _drop("etf_flag", work["is_etf"].fillna(False).astype(bool))
        _drop("etf_or_fund_name", name_series.str.contains(_NAME_BLOCKLIST["etf"]))

    # When-Issued / temporary instruments. Done early and explicitly so a
    # "Common Stock When-Issued" line (e.g. SNDK/CEG in the audited run) is
    # excluded by default rather than ranked as ordinary common stock.
    if not cfg.effective_include("when_issued"):
        _drop("when_issued", name_series.str.contains(_NAME_BLOCKLIST["when_issued"]))

    if not cfg.effective_include("warrants"):
        _drop("warrant_name", name_series.str.contains(_NAME_BLOCKLIST["warrant"]))
        _drop("warrant_suffix", work["ticker"].str.contains(_SUFFIX_PATTERNS["warrant"]))
    if not cfg.effective_include("units"):
        _drop("unit_name", name_series.str.contains(_NAME_BLOCKLIST["unit"]))
        _drop("unit_suffix", work["ticker"].str.contains(_SUFFIX_PATTERNS["unit"]))
    if not cfg.effective_include("preferred"):
        _drop("preferred_name", name_series.str.contains(_NAME_BLOCKLIST["preferred"]))
        _drop("preferred_suffix", work["ticker"].str.contains(_SUFFIX_PATTERNS["preferred"]))
    if not cfg.effective_include("rights"):
        _drop("rights_name", name_series.str.contains(_NAME_BLOCKLIST["rights"]))
        _drop("rights_suffix", work["ticker"].str.contains(_SUFFIX_PATTERNS["rights"]))
    if not cfg.include_notes:
        _drop("notes_name", name_series.str.contains(_NAME_BLOCKLIST["notes"]))

    cleaned = work.loc[keep].copy()
    cleaned["asset_type"] = cleaned.get("asset_type", "common").fillna("common")
    cleaned = cleaned.reset_index(drop=True)

    stats = {
        "raw": int(len(work)),
        "cleaned": int(len(cleaned)),
        "removed": dict(sorted(removed.items(), key=lambda kv: -kv[1])),
    }
    return cleaned, stats


# Normalize the various ways an exchange might be spelled so the YAML
# whitelist matches the source data. Both 'NYSE American' and 'NYSEAMERICAN'
# and 'AMEX' end up at the same canonical key.
_EXCHANGE_ALIASES = {
    "nyseamerican": "nyse american",
    "nyse-american": "nyse american",
    "amex": "nyse american",
    "nysearca": "nyse arca",
    "nyse-arca": "nyse arca",
    "arca": "nyse arca",
    "cboe": "bats",
}


def _canon_exchange(name: str) -> str:
    s = (name or "").strip().lower()
    return _EXCHANGE_ALIASES.get(s, s)


def universe_counts_by_exchange(df: pd.DataFrame) -> dict:
    """Return a dict of exchange -> count, useful for stage 1 reporting."""
    if df.empty or "exchange" not in df.columns:
        return {}
    series = df["exchange"].fillna("UNKNOWN").astype(str)
    return {k: int(v) for k, v in series.value_counts().to_dict().items()}


def save_universe(df: pd.DataFrame, path: str) -> Path:
    """Persist to CSV; create parent directories as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)
    logger.info("Saved universe -> %s (%d rows).", p, len(df))
    return p
