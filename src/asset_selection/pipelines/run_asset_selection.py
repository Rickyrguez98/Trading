"""Staged asset-selection pipeline.

Stages (a free-API-friendly funnel):
    1. Universe collection   -- cleaned U.S. common-stock universe.
    2. Price/liquidity       -- cheap filter; remove illiquid names; rank by
                                dollar volume; keep ``pipeline.after_prices_top_k``.
    3. Fundamental prescreen -- pull fundamentals + score; remove tiny caps;
                                keep ``pipeline.after_fundamentals_top_k``.
    4. News + sentiment      -- only for the post-fundamentals shortlist, so
                                the free yfinance news endpoint isn't hammered.
    5. Composite + rank      -- final score, flags, CSV/Markdown/JSON outputs.

CLI modes:
    --universe full   (default)  -- entire cleaned universe enters stage 1.
    --universe sample            -- stage 1 honours --limit / run.sample_limit.
    --universe custom            -- universe = --tickers value, stages 1-2 are
                                    short-circuited (the user already chose).

Outputs:
    data/processed/universe_full.csv            -- pre-clean raw universe (stage 1 in)
    data/processed/universe_clean.csv           -- post-clean universe (stage 1 out)
    data/processed/asset_selection_results.csv  -- full ranked table
    reports/top_candidates.md                   -- human-readable top-N report
    reports/asset_selection_summary.json        -- machine-readable run summary
    reports/universe_summary.json               -- stage stats + exchange breakdown
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 - .env is optional
    pass

from ..config import AppConfig, load_config
from ..data_providers import (
    build_fundamentals_provider,
    build_news_provider,
    build_prices_provider,
)
from ..data_providers import errors as _err
from ..data_providers.base import Fundamentals, NewsItem, PriceSnapshot
from ..health import run_provider_health_checks
from ..fundamentals.fundamental_scoring import score_fundamentals
from ..logging_config import configure_logging
from ..scoring.allocation_eligibility import (
    allocation_field_summary,
    compute_allocation_fields,
)
from ..scoring.composite_score import (
    compute_composite_scores,
    compute_effective_confidence,
    compute_effective_sentiment,
    compute_risk_penalty,
    flag_rows,
)
from ..scoring.ranking import format_top_candidates_markdown, rank_candidates
from ..sentiment.sentiment_model import (
    aggregate_ticker_sentiment,
    get_sentiment_model,
    score_articles,
)
from ..universe import build_universe, save_universe, universe_counts_by_exchange
from ..validation import (
    assess_coverage,
    build_provider_diagnostics,
    build_provider_report,
    determine_run_status,
    render_provider_provenance_note,
    render_run_status_banner,
    validate_outputs,
    write_provider_diagnostics,
    write_validation_reports,
)
from ..utils.cache import Cache
from ..utils.io import ensure_dir, write_csv, write_json
from ..utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage stats
# ---------------------------------------------------------------------------

@dataclass
class StageStats:
    name: str
    input_count: int = 0
    output_count: int = 0
    duration_seconds: float = 0.0
    # ``provider_failures`` is the count of calls that did NOT return usable
    # data (errors + empty responses). It is deliberately distinct from a
    # genuine economic drop (e.g. a real but illiquid name): an illiquid ticker
    # has status "ok" and is counted under ``dropped``, never here.
    provider_failures: int = 0
    failure_reasons: Dict[str, int] = field(default_factory=dict)  # {"error": n, "empty": n}
    # Richer, machine-readable breakdown by error-taxonomy constant, e.g.
    # {"PROVIDER_JSON_PARSE_ERROR": n, "NO_PRICE_DATA": m}.
    failure_error_types: Dict[str, int] = field(default_factory=dict)
    failures: List[Dict[str, Any]] = field(default_factory=list)   # capped examples
    dropped: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def record_failure(self, ticker: str, provider_symbol: Optional[str],
                       status: str, reason: Optional[str],
                       error_type: Optional[str] = None, cap: int = 50) -> None:
        self.provider_failures += 1
        self.failure_reasons[status] = self.failure_reasons.get(status, 0) + 1
        if error_type:
            self.failure_error_types[error_type] = (
                self.failure_error_types.get(error_type, 0) + 1
            )
        if len(self.failures) < cap:
            self.failures.append({
                "ticker": ticker,
                "provider_symbol": provider_symbol,
                "status": status,
                "error_type": error_type,
                "reason": reason,
            })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "duration_seconds": round(self.duration_seconds, 2),
            "provider_failures": self.provider_failures,
            "failure_reasons": dict(self.failure_reasons),
            "failure_error_types": dict(self.failure_error_types),
            "failures": list(self.failures),
            "dropped": dict(self.dropped),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="asset-selection",
        description=(
            "Rank U.S.-listed common stocks using free fundamentals + sentiment. "
            "By default the full universe enters a staged funnel; news/sentiment "
            "is only collected for the final shortlist to respect free-API limits."
        ),
    )
    p.add_argument(
        "--config", default="configs/default_config.yaml",
        help="Path to YAML config (default: configs/default_config.yaml)."
    )
    p.add_argument(
        "--universe", choices=("full", "sample", "custom"), default=None,
        help=(
            "Universe mode. 'full' (default) runs the staged funnel over the "
            "entire cleaned U.S. universe; 'sample' honours --limit for fast "
            "testing; 'custom' uses --tickers and skips universe build."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help=(
            "Sample-mode only: cap the stage-1 universe at N tickers. "
            "Ignored in 'full' mode and unnecessary in 'custom' mode."
        ),
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="Top-N tickers to include in the Markdown report."
    )
    p.add_argument(
        "--tickers", nargs="*", default=None,
        help="Custom mode: run only on these tickers (implies --universe custom)."
    )
    p.add_argument(
        "--refresh-cache", action="store_true",
        help="Invalidate cached provider responses before running."
    )
    p.add_argument(
        "--no-cache", action="store_true",
        help="Disable caching entirely for this run."
    )
    p.add_argument(
        "--output-dir", default=None,
        help="Override the report output directory."
    )
    p.add_argument(
        "--log-level", default=None,
        help="Override the log level: DEBUG / INFO / WARNING / ERROR."
    )
    p.add_argument(
        "--health-check-only", action="store_true",
        help=(
            "Probe each provider against benchmark mega-caps (AAPL, MSFT, "
            "GOOGL, NVDA, BRK.B), write reports/provider_health.json, and exit "
            "WITHOUT running the full pipeline. Exit 2 on a systemic failure."
        ),
    )
    p.add_argument(
        "--no-provider-health-check", action="store_true",
        help=(
            "Skip the pre-run provider health check. By default the pipeline "
            "probes benchmark tickers first so a systemic provider outage is "
            "caught before a misleading ranking is produced."
        ),
    )
    p.add_argument(
        "--use-cache-on-provider-failure", action="store_true",
        help=(
            "Backup Plan C: when every live provider fails for a ticker, serve a "
            "fresh-enough cached record (within --max-cache-age-days) labeled "
            "'stale_cache' rather than reporting no data. Off by default so a run "
            "never silently passes stale data off as live."
        ),
    )
    p.add_argument(
        "--max-cache-age-days", type=float, default=None,
        help=(
            "Maximum age (in days) of a cache entry allowed to back a failed live "
            "fetch under --use-cache-on-provider-failure. Default: config value "
            "(robustness.max_cache_age_days, 7 days)."
        ),
    )
    p.add_argument(
        "--provider", nargs="*", default=None, metavar="TYPE=NAME[,NAME...]",
        help=(
            "Override the provider(s) for a data type. Repeatable / space-"
            "separated, e.g. '--provider prices=yfinance,stooq fundamentals="
            "yfinance'. A comma-separated list sets the fallback priority order "
            "(first is primary); a single name sets just that provider. Valid "
            "types: prices, fundamentals, news."
        ),
    )
    p.add_argument(
        "--allow-partial-ranking", dest="allow_partial_ranking",
        action="store_true", default=None,
        help=(
            "Present a degraded-but-usable run as a PARTIAL ranking (with "
            "warnings) instead of a diagnostic-only result. On by default; use "
            "--no-partial-ranking to require full coverage."
        ),
    )
    p.add_argument(
        "--no-partial-ranking", dest="allow_partial_ranking",
        action="store_false",
        help=(
            "Refuse to present a ranking when coverage is degraded; emit a "
            "diagnostic-only result instead (exit 2)."
        ),
    )
    return p.parse_args(argv)


def _apply_provider_overrides(args: argparse.Namespace, config: AppConfig) -> None:
    """Apply --provider TYPE=NAME[,NAME...] overrides onto the config.

    A single name sets ``providers.<type>`` (the primary). A comma-separated
    list additionally records the fallback priority order in
    ``robustness.provider_priority_by_data_type[<type>]``.
    """
    if not args.provider:
        return
    valid = {"prices", "fundamentals", "news"}
    for token in args.provider:
        if "=" not in token:
            raise SystemExit(
                f"--provider expects TYPE=NAME[,NAME...]; got {token!r}."
            )
        data_type, _, raw_names = token.partition("=")
        data_type = data_type.strip().lower()
        if data_type not in valid:
            raise SystemExit(
                f"--provider type must be one of {sorted(valid)}; got {data_type!r}."
            )
        names = [n.strip() for n in raw_names.split(",") if n.strip()]
        if not names:
            raise SystemExit(f"--provider {data_type}= requires at least one name.")
        setattr(config.providers, data_type, names[0])
        if len(names) > 1:
            config.robustness.provider_priority_by_data_type[data_type] = names


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def _resolve_mode(args: argparse.Namespace, config: AppConfig) -> str:
    if args.tickers:
        # CLI tickers always force custom mode regardless of --universe.
        return "custom"
    if args.universe:
        return args.universe
    return config.run.mode or "full"


def _resolve_sample_limit(
    args: argparse.Namespace, config: AppConfig, mode: str
) -> Optional[int]:
    if mode != "sample":
        if args.limit is not None and mode != "custom":
            logger.warning(
                "--limit is only effective in --universe sample (mode=%s). Ignoring.",
                mode,
            )
        return None
    # In sample mode, CLI wins, then config, then legacy max_tickers, then None.
    if args.limit is not None:
        return int(args.limit)
    if config.run.sample_limit is not None:
        return int(config.run.sample_limit)
    if config.run.max_tickers is not None:
        return int(config.run.max_tickers)
    return None


# ---------------------------------------------------------------------------
# Stage 1: universe collection
# ---------------------------------------------------------------------------

def _stage1_universe(
    args: argparse.Namespace,
    config: AppConfig,
    cache: Cache,
    rate_limiter: RateLimiter,
    mode: str,
    sample_limit: Optional[int],
) -> "tuple[pd.DataFrame, StageStats]":
    started = time.perf_counter()
    stats = StageStats(name="1_universe")

    processed_dir = ensure_dir(config.run.processed_dir)

    if mode == "custom":
        tickers = [t.strip().upper() for t in (args.tickers or []) if t.strip()]
        df = pd.DataFrame({
            "ticker": tickers,
            "company_name": tickers,
            "exchange": None,
            "asset_type": "common",
            "is_etf": False,
            "is_test_issue": False,
            "source": "cli",
        })
        stats.input_count = len(df)
        stats.output_count = len(df)
        stats.notes.append("custom mode: tickers from --tickers")
        logger.info("Stage 1: %d user-supplied tickers (custom mode).", len(df))
        stats.duration_seconds = time.perf_counter() - started
        return df, stats

    cleaned = build_universe(config, cache=cache, rate_limiter=rate_limiter)

    # Persist both pre- and post-clean snapshots. We do not have the raw
    # pre-clean DF here (build_universe returns cleaned), but the SymbolDirectory
    # provider caches the raw set; save_universe writes the cleaned snapshot.
    save_universe(cleaned, str(processed_dir / "universe_clean.csv"))

    stats.input_count = len(cleaned)
    by_exchange = universe_counts_by_exchange(cleaned)
    for exch, n in by_exchange.items():
        stats.notes.append(f"exchange:{exch}={n}")

    # Per-reason removal accounting from the cleaning step (When-Issued, ETF,
    # preferred, ...). Stored on ``dropped`` so it shows up in the summaries.
    clean_stats = cleaned.attrs.get("clean_stats") if hasattr(cleaned, "attrs") else None
    if isinstance(clean_stats, dict) and clean_stats.get("removed"):
        for reason, n in clean_stats["removed"].items():
            stats.dropped[f"universe_clean:{reason}"] = int(n)
        stats.notes.append(
            f"universe_clean: {clean_stats['raw']} raw -> {clean_stats['cleaned']} cleaned"
        )

    df = cleaned
    if mode == "sample" and sample_limit and len(df) > sample_limit:
        df = df.head(sample_limit).copy()
        stats.dropped["sample_limit"] = len(cleaned) - len(df)
        logger.info(
            "Stage 1: cleaned universe %d -> %d (sample limit %d).",
            len(cleaned), len(df), sample_limit,
        )
    elif config.pipeline.universe_max and len(df) > config.pipeline.universe_max:
        df = df.head(config.pipeline.universe_max).copy()
        stats.dropped["universe_max"] = len(cleaned) - len(df)
        logger.info(
            "Stage 1: cleaned universe %d -> %d (pipeline.universe_max).",
            len(cleaned), len(df),
        )
    else:
        logger.info("Stage 1: cleaned universe = %d tickers.", len(df))

    stats.output_count = len(df)
    stats.duration_seconds = time.perf_counter() - started
    return df, stats


# ---------------------------------------------------------------------------
# Stage 2: cheap price / liquidity filter
# ---------------------------------------------------------------------------

def _stage2_prices(
    universe_df: pd.DataFrame,
    price_provider,
    config: AppConfig,
) -> "tuple[pd.DataFrame, Dict[str, PriceSnapshot], StageStats]":
    started = time.perf_counter()
    stats = StageStats(name="2_prices")
    stats.input_count = len(universe_df)

    price_records: Dict[str, PriceSnapshot] = {}
    rows: List[Dict[str, Any]] = []

    iterator = _progress(universe_df["ticker"].tolist(), desc="Stage 2: prices")
    for ticker in iterator:
        try:
            snap = price_provider.fetch(ticker, lookback_days=config.prices.lookback_days)
        except Exception as exc:  # noqa: BLE001 - report-and-continue
            logger.warning("Stage 2 price fetch raised for %s: %s", ticker, exc)
            snap = PriceSnapshot(
                ticker=ticker, lookback_days=config.prices.lookback_days,
                source=config.providers.prices, status="error",
                error=f"{type(exc).__name__}: {exc}",
                error_type=_err.classify_exception(exc), data_source="unavailable",
            )
        price_records[ticker] = snap
        # Honest failure accounting: a non-"ok" status means the provider gave
        # us no usable data. We record it here so it can never masquerade as a
        # genuine "illiquid" drop below.
        if getattr(snap, "status", "ok") != "ok":
            stats.record_failure(
                ticker, getattr(snap, "provider_symbol", None),
                snap.status, snap.error,
                error_type=getattr(snap, "error_type", None),
            )
        rows.append({
            "ticker": ticker,
            "provider_symbol": getattr(snap, "provider_symbol", ticker),
            "provider_status": getattr(snap, "status", "ok"),
            "last_close": snap.last_close,
            "avg_daily_volume": snap.avg_daily_volume,
            "avg_dollar_volume": snap.avg_dollar_volume,
            "return_pct": snap.return_pct,
            "volatility_pct": snap.volatility_pct,
        })

    df = universe_df.merge(pd.DataFrame(rows), on="ticker", how="left")

    # Separate provider no-data (error/empty) from genuine illiquidity. A
    # ticker the provider couldn't serve is NOT evidence of illiquidity.
    status_col = df.get("provider_status", pd.Series("ok", index=df.index)).fillna("ok")
    no_data = status_col != "ok"
    if int(no_data.sum()):
        stats.dropped["no_provider_data"] = int(no_data.sum())
    df = df.loc[~no_data].copy()

    # Liquidity filter (only over names the provider actually priced).
    adv = pd.to_numeric(df["avg_dollar_volume"], errors="coerce")
    illiquid = adv.fillna(0) < config.prices.min_avg_dollar_volume
    stats.dropped["below_min_dollar_volume"] = int(illiquid.sum())
    df = df.loc[~illiquid].copy()

    # Minimum price-history filter -- we use volatility_pct as the existence
    # proxy: it's only computed when at least 5 daily returns existed, so a
    # missing volatility for non-zero lookback indicates a very thin tape.
    if config.pipeline.min_price_history_days > 0:
        has_history = pd.to_numeric(df.get("volatility_pct"), errors="coerce").notna()
        stats.dropped["insufficient_price_history"] = int((~has_history).sum())
        df = df.loc[has_history].copy()

    # Rank by dollar volume and keep top-K.
    if config.pipeline.after_prices_top_k and len(df) > config.pipeline.after_prices_top_k:
        df = df.sort_values("avg_dollar_volume", ascending=False).head(
            config.pipeline.after_prices_top_k
        ).copy()
        stats.notes.append(f"after_prices_top_k={config.pipeline.after_prices_top_k}")

    stats.output_count = len(df)
    stats.duration_seconds = time.perf_counter() - started
    logger.info(
        "Stage 2: prices %d -> %d (liquidity + top-K).",
        stats.input_count, stats.output_count,
    )
    return df, price_records, stats


# ---------------------------------------------------------------------------
# Stage 3: fundamentals prescreen
# ---------------------------------------------------------------------------

def _stage3_fundamentals(
    df: pd.DataFrame,
    fund_provider,
    config: AppConfig,
) -> "tuple[pd.DataFrame, List[Fundamentals], StageStats]":
    started = time.perf_counter()
    stats = StageStats(name="3_fundamentals")
    stats.input_count = len(df)

    records: List[Fundamentals] = []
    iterator = _progress(df["ticker"].tolist(), desc="Stage 3: fundamentals")
    for ticker in iterator:
        try:
            f = fund_provider.fetch(ticker)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage 3 fundamentals fetch raised for %s: %s", ticker, exc)
            f = Fundamentals(
                ticker=ticker, source=config.providers.fundamentals,
                status="error", error=f"{type(exc).__name__}: {exc}",
                error_type=_err.classify_exception(exc), data_source="unavailable",
            )
        if getattr(f, "status", "ok") != "ok":
            # Fundamentals that came back empty/errored are still carried
            # forward (missing data is penalized, not dropped) but we count the
            # provider miss honestly so the summary can't claim zero failures.
            stats.record_failure(
                ticker, getattr(f, "provider_symbol", None), f.status, f.error,
                error_type=getattr(f, "error_type", None),
            )
        records.append(f)

    fund_scores = score_fundamentals(records, config.scoring)

    # Build the metadata frame and merge.
    meta = pd.DataFrame([{
        "ticker": f.ticker,
        "company_name_fund": f.company_name,
        "sector": f.sector,
        "industry": f.industry,
        "market_cap": f.market_cap,
        "missing_fields": f.missing_fields,
    } for f in records])

    merged = df.merge(meta, on="ticker", how="left")
    if not fund_scores.empty:
        merged = merged.merge(fund_scores, on="ticker", how="left")

    # Prefer the fundamentals-sourced company name if we don't have one yet.
    if "company_name" in merged.columns and "company_name_fund" in merged.columns:
        merged["company_name"] = merged["company_name"].where(
            merged["company_name"].astype(str).str.len() > 0,
            merged["company_name_fund"],
        )
    if "company_name_fund" in merged.columns:
        merged = merged.drop(columns=["company_name_fund"])

    # Minimum market cap (only if available).
    mc = pd.to_numeric(merged.get("market_cap"), errors="coerce")
    below_mc = mc.fillna(0) < config.prices.min_market_cap
    # Don't drop names where market_cap is *missing* here -- many small caps
    # will lack market_cap from yfinance. We treat missing as "unknown, keep".
    drop_mask = below_mc & mc.notna()
    stats.dropped["below_min_market_cap"] = int(drop_mask.sum())
    merged = merged.loc[~drop_mask].copy()

    # Fill defaults so we can rank cleanly.
    for col in ("fundamentals_score", "growth_score", "quality_score",
                "valuation_score", "balance_sheet_score", "cash_flow_score"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce").fillna(50.0)
    if "missing_metric_count" in merged.columns:
        merged["missing_metric_count"] = pd.to_numeric(
            merged["missing_metric_count"], errors="coerce"
        ).fillna(0).astype(int)

    # Keep top-K by fundamentals_score; if missing, fall back to no rank.
    if (
        config.pipeline.after_fundamentals_top_k
        and "fundamentals_score" in merged.columns
        and len(merged) > config.pipeline.after_fundamentals_top_k
    ):
        merged = merged.sort_values("fundamentals_score", ascending=False).head(
            config.pipeline.after_fundamentals_top_k
        ).copy()
        stats.notes.append(f"after_fundamentals_top_k={config.pipeline.after_fundamentals_top_k}")

    # Filter the records list down to the shortlist.
    keep_tickers = set(merged["ticker"].tolist())
    records = [r for r in records if r.ticker in keep_tickers]

    stats.output_count = len(merged)
    stats.duration_seconds = time.perf_counter() - started
    logger.info(
        "Stage 3: fundamentals %d -> %d (market-cap + top-K).",
        stats.input_count, stats.output_count,
    )
    return merged, records, stats


# ---------------------------------------------------------------------------
# Stage 4: news + sentiment
# ---------------------------------------------------------------------------

def _stage4_sentiment(
    df: pd.DataFrame,
    news_provider,
    sentiment_model,
    config: AppConfig,
) -> "tuple[pd.DataFrame, StageStats]":
    started = time.perf_counter()
    stats = StageStats(name="4_sentiment")
    stats.input_count = len(df)

    scfg = config.sentiment
    model_factor = (
        scfg.finbert_confidence_factor
        if scfg.model == "finbert"
        else scfg.vader_confidence_factor
    )

    sentiment_rows: List[Dict[str, Any]] = []
    iterator = _progress(df["ticker"].tolist(), desc="Stage 4: news+sentiment")
    for ticker in iterator:
        try:
            articles = news_provider.fetch(ticker, max_age_days=scfg.max_age_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage 4 news fetch raised for %s: %s", ticker, exc)
            articles = []
            stats.record_failure(
                ticker, None, "error", f"{type(exc).__name__}: {exc}",
                error_type=_err.classify_exception(exc),
            )

        scored = score_articles(articles, sentiment_model)
        agg = aggregate_ticker_sentiment(
            ticker,
            scored,
            recency_halflife_days=scfg.recency_halflife_days,
            min_articles_for_confidence=scfg.min_articles_for_confidence,
            confidence_full_article_count=scfg.confidence_full_article_count,
            confidence_full_source_count=scfg.confidence_full_source_count,
            stale_after_days=scfg.stale_after_days,
            model_confidence_factor=model_factor,
            model_name=scfg.model,
        )
        sentiment_rows.append({
            "ticker": ticker,
            "sentiment_score": agg.sentiment_score,
            "article_count": agg.article_count,
            "unique_article_count": agg.unique_article_count,
            "duplicate_count": agg.duplicate_count,
            "stale_count": agg.stale_count,
            "fresh_ratio": agg.fresh_ratio,
            "unique_ratio": agg.unique_ratio,
            "positive_ratio": agg.positive_ratio,
            "negative_ratio": agg.negative_ratio,
            "neutral_ratio": agg.neutral_ratio,
            "source_diversity": agg.source_diversity,
            "sentiment_confidence": agg.confidence,
            "sentiment_model": agg.model_name,
            "news_titles": [a.headline for a in agg.articles[:10]],
        })

    sentiment_df = pd.DataFrame(sentiment_rows)
    merged = df.merge(sentiment_df, on="ticker", how="left")

    # Default fills for tickers that came back with nothing.
    merged["sentiment_score"] = merged["sentiment_score"].fillna(50.0)
    merged["article_count"] = merged["article_count"].fillna(0).astype(int)
    for col, default in (
        ("unique_article_count", 0),
        ("duplicate_count", 0),
        ("stale_count", 0),
        ("source_diversity", 0),
    ):
        if col in merged.columns:
            merged[col] = merged[col].fillna(default).astype(int)
    for col, default in (("fresh_ratio", 0.0), ("unique_ratio", 0.0)):
        if col in merged.columns:
            merged[col] = merged[col].fillna(default)
    if "sentiment_confidence" in merged.columns:
        merged["sentiment_confidence"] = merged["sentiment_confidence"].fillna(0.0)

    stats.output_count = len(merged)
    stats.duration_seconds = time.perf_counter() - started
    logger.info("Stage 4: sentiment computed for %d tickers.", stats.output_count)
    return merged, stats


# ---------------------------------------------------------------------------
# Stage 5: compose + rank
# ---------------------------------------------------------------------------

def _stage5_compose_and_rank(
    df: pd.DataFrame, config: AppConfig
) -> "tuple[pd.DataFrame, StageStats]":
    started = time.perf_counter()
    stats = StageStats(name="5_compose_and_rank")
    stats.input_count = len(df)

    df = df.copy()
    df["risk_penalty"] = compute_risk_penalty(df, config.prices)
    # Keep the raw sentiment for transparency, and derive a confidence-adjusted
    # effective sentiment that the composite consumes by default. A low-confidence
    # or stale-news sentiment is pulled toward neutral instead of swinging the score.
    df["raw_sentiment_score"] = pd.to_numeric(
        df.get("sentiment_score", config.sentiment.neutral_sentiment_score),
        errors="coerce",
    ).fillna(config.sentiment.neutral_sentiment_score)
    # Stale news damps the confidence used by the effective-sentiment formula.
    df["effective_sentiment_confidence"] = compute_effective_confidence(df, config.sentiment)
    df["effective_sentiment_score"] = compute_effective_sentiment(df, config.sentiment)
    sentiment_column = (
        "effective_sentiment_score"
        if config.sentiment.use_confidence_adjusted_sentiment
        else "sentiment_score"
    )
    df["final_score"] = compute_composite_scores(
        df, config.composite, sentiment_column=sentiment_column
    )
    df = flag_rows(
        df,
        config.composite,
        low_sentiment_confidence_threshold=config.sentiment.low_confidence_threshold,
        weak_return_threshold=config.prices.weak_return_threshold,
        risk_controls=config.risk_controls,
        stale_news_fresh_ratio_threshold=config.sentiment.stale_news_fresh_ratio_threshold,
        very_stale_news_fresh_ratio_threshold=config.sentiment.very_stale_news_fresh_ratio_threshold,
        low_source_diversity_threshold=config.sentiment.low_source_diversity_threshold,
    )
    ranked = rank_candidates(df, top_n=config.run.top_n)
    # Separate the research ranking from the allocation shortlist: tag every
    # candidate with eligibility + an allocation-adjusted score (never drops rows).
    ranked = compute_allocation_fields(
        ranked,
        allocation_cfg=config.allocation,
        risk_controls=config.risk_controls,
        sentiment_cfg=config.sentiment,
    )

    stats.output_count = len(ranked)
    stats.duration_seconds = time.perf_counter() - started
    return ranked, stats


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config)

    # CLI overrides
    if args.no_cache:
        config.cache.enabled = False
    if args.top is not None:
        config.run.top_n = args.top
    if args.output_dir:
        config.run.output_dir = args.output_dir
    if args.use_cache_on_provider_failure:
        config.robustness.use_cache_on_provider_failure = True
    if args.max_cache_age_days is not None:
        config.robustness.max_cache_age_days = float(args.max_cache_age_days)
    if args.allow_partial_ranking is not None:
        config.robustness.allow_partial_ranking = bool(args.allow_partial_ranking)
    _apply_provider_overrides(args, config)

    configure_logging(
        level=args.log_level or config.logging.level,
        log_to_file=config.logging.log_to_file,
        file=config.logging.file,
    )

    mode = _resolve_mode(args, config)
    sample_limit = _resolve_sample_limit(args, config, mode)

    pipeline_started = time.perf_counter()
    logger.info("Asset selection pipeline started at %s", datetime.utcnow().isoformat())
    logger.info(
        "Mode=%s | sample_limit=%s | providers: fundamentals=%s prices=%s news=%s | sentiment=%s",
        mode, sample_limit,
        config.providers.fundamentals,
        config.providers.prices,
        config.providers.news,
        config.sentiment.model,
    )
    if mode == "full":
        logger.info(
            "Full-universe mode: this can take a while. Stage 2 reduces to "
            "after_prices_top_k=%s; stage 3 to after_fundamentals_top_k=%s; "
            "news/sentiment only runs on the stage-3 shortlist (free-API friendly).",
            config.pipeline.after_prices_top_k,
            config.pipeline.after_fundamentals_top_k,
        )

    cache = Cache(directory=config.cache.dir, enabled=config.cache.enabled)
    if args.refresh_cache and cache.enabled:
        removed = cache.invalidate()
        logger.info("Cleared cache: %d entries removed.", removed)

    # Universe building still uses a single default-paced limiter.
    rate_limiter = RateLimiter(config.rate_limits.get("yfinance", 0.4))

    # Providers are built from config priority order (primary -> fallback) with
    # optional cache-backup, so the stages talk to a chain without knowing it.
    # The builders take callbacks so this module owns cache/pacing wiring.
    _limiters: Dict[str, RateLimiter] = {}

    def make_cache(namespace: str) -> Cache:
        return _namespaced_cache(cache, namespace, config)

    def make_rate_limiter(name: str) -> RateLimiter:
        # One limiter per provider name so chained providers don't share pace.
        if name not in _limiters:
            default = config.rate_limits.get("yfinance", 0.4)
            _limiters[name] = RateLimiter(config.rate_limits.get(name, default))
        return _limiters[name]

    fund_provider = build_fundamentals_provider(config, make_cache, make_rate_limiter)
    price_provider = build_prices_provider(config, make_cache, make_rate_limiter)
    news_provider = build_news_provider(config, make_cache, make_rate_limiter)

    try:
        sentiment_model = get_sentiment_model(config.sentiment.model)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sentiment model %s failed to load (%s); falling back to vader.",
            config.sentiment.model, exc,
        )
        sentiment_model = get_sentiment_model("vader")

    # --- Provider health check (benchmark mega-caps) ---
    health_report: Optional[Dict[str, Any]] = None
    run_health = args.health_check_only or not args.no_provider_health_check
    if run_health:
        logger.info("Running provider health checks on benchmark tickers ...")
        health_report = run_provider_health_checks(
            price_provider=price_provider,
            fundamentals_provider=fund_provider,
            news_provider=news_provider,
        )
        output_dir = ensure_dir(config.run.output_dir)
        write_json(output_dir / "provider_health.json", health_report)
        logger.info(
            "Provider health: %s (price_systemic=%s, fundamentals_systemic=%s)",
            health_report.get("overall_status"),
            health_report.get("price_systemic_failure"),
            health_report.get("fundamentals_systemic_failure"),
        )

    if args.health_check_only:
        if health_report is None:  # pragma: no cover - defensive
            return 1
        systemic = bool(health_report.get("any_blocking_systemic_failure"))
        logger.info(
            "Health-check-only mode: overall=%s. %s",
            health_report.get("overall_status"),
            "Systemic provider failure detected -- a full run would NOT produce a "
            "trusted ranking." if systemic else "Providers look usable.",
        )
        return 2 if systemic else 0

    # --- Refuse to run a full funnel during a confirmed systemic outage ---
    # If the benchmark mega-caps could not be fetched, re-ranking the survivors
    # would be misleading (it's a provider outage, not a set of bad tickers).
    # We emit a diagnostic summary and stop rather than burn API calls.
    if (
        health_report
        and health_report.get("any_blocking_systemic_failure")
        and config.robustness.stop_on_systemic_provider_failure
    ):
        coverage = assess_coverage([], None, config)
        status = determine_run_status(coverage, health_report, config)
        output_dir = ensure_dir(config.run.output_dir)
        fallback_usage = _fallback_usage_summary({
            "prices": price_provider,
            "fundamentals": fund_provider,
            "news": news_provider,
        })
        configured_providers = {
            "fundamentals": config.providers.fundamentals,
            "prices": config.providers.prices,
            "news": config.providers.news,
        }
        provider_report = build_provider_report(
            configured_providers=configured_providers,
            fallback_usage=fallback_usage,
            cache_usage={},
        )
        summary = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "mode": mode,
            "run_status": status["run_status"],
            "ranking_validity": status["ranking_validity"],
            "invalid_ranking_reasons": status["invalid_ranking_reasons"],
            "coverage_warnings": status["warnings"],
            "recommendations_for_next_run": status["recommendations_for_next_run"],
            "data_coverage_summary": coverage,
            "fallback_usage_summary": fallback_usage,
            "provider_report": provider_report,
            "provider_health_check_summary": health_report,
            "candidates": [],
            "stages": [],
        }
        write_json(output_dir / "asset_selection_summary.json", summary)
        # A banner-only Markdown report so reports/top_candidates.md visibly says
        # "not a ranking" rather than being stale or absent.
        (output_dir / "top_candidates.md").write_text(
            render_run_status_banner(status, coverage)
            + "# Asset Selection — Top Candidates\n\n"
            "_No ranking produced: systemic provider failure. "
            "See `provider_diagnostics.md`._\n",
            encoding="utf-8",
        )
        diag = build_provider_diagnostics(
            status=status, coverage=coverage, health_report=health_report,
            provider_failures={}, fallback_usage=fallback_usage, cache_usage={},
            providers=configured_providers,
        )
        write_provider_diagnostics(diag, output_dir)
        logger.error(
            "Systemic provider failure on benchmark mega-caps -> %s. Refusing to "
            "produce a ranking; wrote a diagnostic summary instead. Reasons: %s",
            status["ranking_validity"],
            "; ".join(status["invalid_ranking_reasons"]) or "(none)",
        )
        return status["return_code"]

    # --- Stages ---
    stage_stats: List[StageStats] = []

    universe_df, s1 = _stage1_universe(
        args, config, cache, rate_limiter, mode, sample_limit
    )
    stage_stats.append(s1)
    exchange_breakdown = universe_counts_by_exchange(universe_df)

    if universe_df.empty:
        logger.error("Universe is empty after stage 1. Exiting.")
        _write_universe_summary(config, mode, exchange_breakdown, stage_stats)
        return 1

    after_prices_df, price_records, s2 = _stage2_prices(
        universe_df, price_provider, config
    )
    stage_stats.append(s2)

    if after_prices_df.empty:
        logger.error("No tickers survived stage 2. Exiting.")
        _write_universe_summary(config, mode, exchange_breakdown, stage_stats)
        return 1

    after_fund_df, fund_records, s3 = _stage3_fundamentals(
        after_prices_df, fund_provider, config
    )
    stage_stats.append(s3)

    if after_fund_df.empty:
        logger.error("No tickers survived stage 3. Exiting.")
        _write_universe_summary(config, mode, exchange_breakdown, stage_stats)
        return 1

    after_sentiment_df, s4 = _stage4_sentiment(
        after_fund_df, news_provider, sentiment_model, config
    )
    stage_stats.append(s4)

    ranked, s5 = _stage5_compose_and_rank(after_sentiment_df, config)
    stage_stats.append(s5)

    # --- Outputs ---
    processed_dir = ensure_dir(config.run.processed_dir)
    output_dir = ensure_dir(config.run.output_dir)

    csv_path = processed_dir / "asset_selection_results.csv"
    md_path = output_dir / "top_candidates.md"
    json_path = output_dir / "asset_selection_summary.json"

    write_csv(csv_path, ranked)

    total_runtime = time.perf_counter() - pipeline_started

    summary = _build_summary(
        ranked, config, mode=mode, sample_limit=sample_limit,
        stage_stats=stage_stats, exchange_breakdown=exchange_breakdown,
        total_runtime=total_runtime,
    )

    # --- Ranking-validity gating: is this ranking trustworthy? ---
    # Coverage + benchmark health decide whether the output is a VALID ranking,
    # a PARTIAL one (with warnings), or merely DIAGNOSTIC/INVALID. This is what
    # stops a systemic provider outage from masquerading as a clean ranking.
    coverage = assess_coverage(stage_stats, ranked, config)
    status = determine_run_status(coverage, health_report, config)

    # Provider-chain + provenance summaries (improvements #7, #9).
    fallback_usage = _fallback_usage_summary({
        "prices": price_provider,
        "fundamentals": fund_provider,
        "news": news_provider,
    })
    cache_usage = _cache_usage_summary(price_records, fund_records)
    # One consistent provenance block reused by the summary JSON, the diagnostics
    # report, and the top-candidates footer so the four artifacts can't disagree.
    configured_providers = {
        "fundamentals": config.providers.fundamentals,
        "prices": config.providers.prices,
        "news": config.providers.news,
    }
    provider_report = build_provider_report(
        configured_providers=configured_providers,
        fallback_usage=fallback_usage,
        cache_usage=cache_usage,
    )

    summary["run_status"] = status["run_status"]
    summary["ranking_validity"] = status["ranking_validity"]
    summary["invalid_ranking_reasons"] = status["invalid_ranking_reasons"]
    summary["coverage_warnings"] = status["warnings"]
    summary["recommendations_for_next_run"] = status["recommendations_for_next_run"]
    summary["data_coverage_summary"] = coverage
    summary["fallback_usage_summary"] = fallback_usage
    summary["cache_usage_summary"] = cache_usage
    # Consolidated provider provenance (improvement #7): configured_providers,
    # provider_chain_by_data_type, actual_provider_usage, cache_usage_by_stage.
    summary["provider_report"] = provider_report
    # Output-location pointers (improvement #8): the summary holds only the
    # top-N slice, so name where the full ranking and human report live.
    summary["full_results_path"] = str(csv_path)
    summary["top_candidates_path"] = str(md_path)
    if health_report is not None:
        summary["provider_health_check_summary"] = health_report
    write_json(json_path, summary)

    # Markdown report, prefixed with a run-status banner so the headline table
    # can never be read without its validity caveat, and suffixed with the same
    # provider-provenance block the diagnostics report uses (consistency).
    banner = render_run_status_banner(status, coverage)
    md = (
        banner
        + format_top_candidates_markdown(ranked, top_n=config.run.top_n)
        + render_provider_provenance_note(provider_report)
    )
    md_path.write_text(md, encoding="utf-8")

    # Consolidated provider diagnostics report (improvement #9) -- the one
    # artifact that says, in plain language, whether today's run is trustworthy.
    diag = build_provider_diagnostics(
        status=status,
        coverage=coverage,
        health_report=health_report,
        provider_failures=summary.get("provider_failures", {}),
        fallback_usage=fallback_usage,
        cache_usage=cache_usage,
        providers={
            "fundamentals": config.providers.fundamentals,
            "prices": config.providers.prices,
            "news": config.providers.news,
        },
    )
    diag_json, diag_md = write_provider_diagnostics(diag, output_dir)
    logger.info("Diagnostics  : %s", diag_md)

    _write_universe_summary(config, mode, exchange_breakdown, stage_stats)

    # Post-run output validation: re-audit the produced candidates and write
    # reports/output_validation.{json,md}. This never drops rows -- it reports.
    try:
        validation = validate_outputs(ranked, summary, config)
        val_json, val_md = write_validation_reports(validation, output_dir)
        logger.info(
            "Validation   : %s (%d warning[s]) -> %s",
            validation.get("overall_status"), validation.get("n_warnings", 0), val_md,
        )
    except Exception as exc:  # noqa: BLE001 - validation must never break a run
        logger.warning("Output validation failed to run: %s", exc)

    logger.info("CSV written  : %s", csv_path)
    logger.info("Markdown     : %s", md_path)
    logger.info("JSON summary : %s", json_path)
    logger.info("Total runtime: %.1fs", total_runtime)
    logger.info(
        "Run status   : %s (ranking_validity=%s).%s",
        status["run_status"], status["ranking_validity"],
        ("" if status["is_trusted"] else
         " Output is NOT a trusted ranking -- see invalid_ranking_reasons."),
    )
    if status["invalid_ranking_reasons"]:
        for r in status["invalid_ranking_reasons"]:
            logger.warning("  reason: %s", r)
    return status["return_code"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _namespaced_cache(cache: Cache, namespace: str, config: AppConfig) -> Cache:
    """Return a cache view with the right TTL for ``namespace``."""
    ttl = config.cache.ttl_seconds.get(namespace, cache.default_ttl)
    return Cache(directory=str(cache.directory), enabled=cache.enabled, default_ttl=ttl)


def _progress(items: Iterable, desc: str):
    try:
        from tqdm import tqdm

        return tqdm(list(items), desc=desc)
    except Exception:  # noqa: BLE001
        return items


def _provenance_counts(records: Iterable) -> Dict[str, int]:
    """Tally the ``data_source`` provenance label across fetched records."""
    out: Dict[str, int] = {}
    for r in records:
        ds = getattr(r, "data_source", None) or "unknown"
        out[ds] = out.get(ds, 0) + 1
    return out


def _fallback_usage_summary(providers: Dict[str, Any]) -> Dict[str, Any]:
    """Per-data-type view of the provider chain and how often a backup fired.

    A single (unwrapped) provider reports ``wrapped: False`` with zeroed
    counters; a Fallback* wrapper exposes its ``usage`` counters and chain.
    """
    out: Dict[str, Any] = {}
    for dtype, p in providers.items():
        usage = getattr(p, "usage", None)
        chain = getattr(p, "provider_names", None) or [getattr(p, "name", "?")]
        if isinstance(usage, dict):
            out[dtype] = {
                "chain": list(chain),
                "wrapped": True,
                "primary": usage.get("primary", 0),
                "fallback": usage.get("fallback", 0),
                "stale_cache": usage.get("stale_cache", 0),
                "unavailable": usage.get("unavailable", 0),
                "by_provider": dict(usage.get("by_provider", {})),
            }
        else:
            out[dtype] = {"chain": list(chain), "wrapped": False}
    return out


def _cache_usage_summary(
    price_records: Dict[str, Any], fund_records: Iterable
) -> Dict[str, Any]:
    """Cache/live provenance for the records we retained (improvement #7).

    ``fresh_cache`` = served from a valid cache entry; ``stale_cache`` = a
    knowingly-expired entry used as a backup; ``live``/``fallback`` = fetched
    this run; ``unavailable`` = no data. News carries no per-item provenance,
    so it is reported via fallback usage instead.
    """
    return {
        "prices": _provenance_counts(price_records.values()),
        "fundamentals": _provenance_counts(fund_records),
    }


def _provider_failure_summary(stage_stats: List[StageStats]) -> Dict[str, Any]:
    """Aggregate per-stage provider failures into one honest block.

    Distinguishes ``error`` (the call raised) from ``empty`` (the call
    succeeded but returned no usable data, e.g. an unsupported/delisted
    symbol). A genuinely illiquid name is NOT counted here -- it has status
    "ok" and lives in the stage's ``dropped`` block instead.
    """
    by_stage: Dict[str, Any] = {}
    total = 0
    total_reasons: Dict[str, int] = {}
    total_error_types: Dict[str, int] = {}
    examples: List[Dict[str, Any]] = []
    for s in stage_stats:
        if s.provider_failures:
            by_stage[s.name] = {
                "count": s.provider_failures,
                "reasons": dict(s.failure_reasons),
                "error_types": dict(s.failure_error_types),
            }
            total += s.provider_failures
            for k, v in s.failure_reasons.items():
                total_reasons[k] = total_reasons.get(k, 0) + v
            for k, v in s.failure_error_types.items():
                total_error_types[k] = total_error_types.get(k, 0) + v
            for f in s.failures:
                examples.append({"stage": s.name, **f})
    # A failure looks systemic when provider-side faults (JSON-parse, rate
    # limit, timeout, blocked, HTTP) dominate over honest "no data" misses.
    provider_side = sum(
        v for k, v in total_error_types.items() if _err.is_provider_side(k)
    )
    return {
        "total": total,
        "by_reason": total_reasons,
        "by_error_type": total_error_types,
        "provider_side_failures": provider_side,
        "by_stage": by_stage,
        "examples": examples[:100],
    }


def _write_universe_summary(
    config: AppConfig,
    mode: str,
    exchange_breakdown: Dict[str, int],
    stage_stats: List[StageStats],
) -> None:
    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "exchange_breakdown": exchange_breakdown,
        "provider_failures": _provider_failure_summary(stage_stats),
        "stages": [s.to_dict() for s in stage_stats],
    }
    output_dir = ensure_dir(config.run.output_dir)
    write_json(output_dir / "universe_summary.json", payload)


def _build_summary(
    ranked: pd.DataFrame,
    config: AppConfig,
    *,
    mode: str,
    sample_limit: Optional[int],
    stage_stats: List[StageStats],
    exchange_breakdown: Dict[str, int],
    total_runtime: float,
) -> dict:
    if ranked.empty:
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "mode": mode,
            "candidates": [],
            "provider_failures": _provider_failure_summary(stage_stats),
            "stages": [s.to_dict() for s in stage_stats],
            "exchange_breakdown": exchange_breakdown,
            "total_runtime_seconds": round(total_runtime, 2),
        }

    top = ranked.head(config.run.top_n).copy()
    candidates: List[Dict[str, Any]] = []
    for _, row in top.iterrows():
        candidate = {
            "rank": int(row.get("rank", 0)),
            "ticker": row.get("ticker"),
            "company_name": row.get("company_name"),
            "sector": row.get("sector"),
            "industry": row.get("industry"),
            "market_cap": _safe_num(row.get("market_cap")),
            "avg_dollar_volume": _safe_num(row.get("avg_dollar_volume")),
            "last_close": _safe_num(row.get("last_close")),
            "return_pct": _safe_num(row.get("return_pct")),
            "volatility_pct": _safe_num(row.get("volatility_pct")),
            "sentiment_score": _safe_num(row.get("sentiment_score")),
            "raw_sentiment_score": _safe_num(row.get("raw_sentiment_score")),
            "effective_sentiment_score": _safe_num(row.get("effective_sentiment_score")),
            "sentiment_article_count": int(row.get("article_count", 0) or 0),
            "sentiment_unique_article_count": int(row.get("unique_article_count", 0) or 0),
            "sentiment_duplicate_count": int(row.get("duplicate_count", 0) or 0),
            "sentiment_stale_count": int(row.get("stale_count", 0) or 0),
            "sentiment_fresh_ratio": _safe_num(row.get("fresh_ratio")),
            "sentiment_unique_ratio": _safe_num(row.get("unique_ratio")),
            "sentiment_positive_ratio": _safe_num(row.get("positive_ratio")),
            "sentiment_negative_ratio": _safe_num(row.get("negative_ratio")),
            "sentiment_confidence": _safe_num(row.get("sentiment_confidence")),
            "sentiment_effective_confidence": _safe_num(row.get("effective_sentiment_confidence")),
            "sentiment_source_diversity": int(row.get("source_diversity", 0) or 0),
            "sentiment_model": row.get("sentiment_model") or config.sentiment.model,
            "fundamentals_score": _safe_num(row.get("fundamentals_score")),
            "growth_score": _safe_num(row.get("growth_score")),
            "quality_score": _safe_num(row.get("quality_score")),
            "valuation_score": _safe_num(row.get("valuation_score")),
            "balance_sheet_score": _safe_num(row.get("balance_sheet_score")),
            "cash_flow_score": _safe_num(row.get("cash_flow_score")),
            "risk_penalty": _safe_num(row.get("risk_penalty")),
            "final_score": _safe_num(row.get("final_score")),
            "top_driver_pillar": row.get("top_driver_pillar") or None,
            "top_drag_pillar": row.get("top_drag_pillar") or None,
            "strongest_metric": row.get("strongest_metric") or None,
            "strongest_metric_score": _safe_num(row.get("strongest_metric_score")),
            "weakest_metric": row.get("weakest_metric") or None,
            "weakest_metric_score": _safe_num(row.get("weakest_metric_score")),
            "market_cap_available": bool(row.get("market_cap_available"))
            if row.get("market_cap_available") is not None else None,
            "valuation_metrics_available": int(row.get("valuation_metrics_available", 0) or 0),
            "selection_bucket": row.get("selection_bucket") or None,
            "reason": row.get("reason"),
            "warning_flags": list(row.get("flags") or []),
            "missing_fields": list(row.get("missing_fields") or []),
            "missing_metric_count": int(row.get("missing_metric_count", 0) or 0),
        }
        # Allocation-eligibility fields (same set in CSV/JSON/Markdown).
        candidate.update(allocation_field_summary(row))
        candidates.append(candidate)
    # Count-of-record fields (improvement #8) make it unambiguous that
    # ``candidates`` holds only the reported top-N slice of a larger ranking,
    # and how many of those names are allocation-eligible.
    eligible_col = ranked.get("eligible_for_allocation")
    ranked_eligible_count = (
        int(pd.Series(eligible_col).astype(bool).sum())
        if eligible_col is not None else 0
    )
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "sample_limit": sample_limit,
        "top_n": int(config.run.top_n),
        # --- Count-of-record fields (improvement #8) ---
        # ``ranked_candidate_count`` is the full research ranking; ``candidates``
        # below is only the top-N slice actually serialized into this summary.
        "ranked_candidate_count": int(len(ranked)),
        "reported_candidate_count": len(candidates),
        "allocation_eligible_count": ranked_eligible_count,
        "providers": {
            "fundamentals": config.providers.fundamentals,
            "prices": config.providers.prices,
            "news": config.providers.news,
        },
        "sentiment_model": config.sentiment.model,
        "weights": config.composite.weights,
        "exchange_breakdown": exchange_breakdown,
        "provider_failures": _provider_failure_summary(stage_stats),
        "stages": [s.to_dict() for s in stage_stats],
        "total_runtime_seconds": round(total_runtime, 2),
        # ``candidates`` is the reported top-N slice (see reported_candidate_count),
        # NOT the full ranking. The complete ranked table lives at
        # ``full_results_path``; the human report at ``top_candidates_path``.
        "candidates": candidates,
    }


def _safe_num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return x


if __name__ == "__main__":
    sys.exit(main())
