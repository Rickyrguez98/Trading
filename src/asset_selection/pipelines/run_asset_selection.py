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
    get_fundamentals_provider,
    get_news_provider,
    get_prices_provider,
)
from ..data_providers.base import Fundamentals, NewsItem, PriceSnapshot
from ..fundamentals.fundamental_scoring import score_fundamentals
from ..logging_config import configure_logging
from ..scoring.composite_score import (
    compute_composite_scores,
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
    failures: List[Dict[str, Any]] = field(default_factory=list)   # capped examples
    dropped: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def record_failure(self, ticker: str, provider_symbol: Optional[str],
                       status: str, reason: Optional[str], cap: int = 50) -> None:
        self.provider_failures += 1
        self.failure_reasons[status] = self.failure_reasons.get(status, 0) + 1
        if len(self.failures) < cap:
            self.failures.append({
                "ticker": ticker,
                "provider_symbol": provider_symbol,
                "status": status,
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
    return p.parse_args(argv)


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
            )
        price_records[ticker] = snap
        # Honest failure accounting: a non-"ok" status means the provider gave
        # us no usable data. We record it here so it can never masquerade as a
        # genuine "illiquid" drop below.
        if getattr(snap, "status", "ok") != "ok":
            stats.record_failure(
                ticker, getattr(snap, "provider_symbol", None),
                snap.status, snap.error,
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
            )
        if getattr(f, "status", "ok") != "ok":
            # Fundamentals that came back empty/errored are still carried
            # forward (missing data is penalized, not dropped) but we count the
            # provider miss honestly so the summary can't claim zero failures.
            stats.record_failure(
                ticker, getattr(f, "provider_symbol", None), f.status, f.error,
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

    sentiment_rows: List[Dict[str, Any]] = []
    iterator = _progress(df["ticker"].tolist(), desc="Stage 4: news+sentiment")
    for ticker in iterator:
        try:
            articles = news_provider.fetch(ticker, max_age_days=config.sentiment.max_age_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage 4 news fetch raised for %s: %s", ticker, exc)
            articles = []
            stats.record_failure(ticker, None, "error", f"{type(exc).__name__}: {exc}")

        scored = score_articles(articles, sentiment_model)
        agg = aggregate_ticker_sentiment(
            ticker,
            scored,
            recency_halflife_days=config.sentiment.recency_halflife_days,
            min_articles_for_confidence=config.sentiment.min_articles_for_confidence,
        )
        sentiment_rows.append({
            "ticker": ticker,
            "sentiment_score": agg.sentiment_score,
            "article_count": agg.article_count,
            "positive_ratio": agg.positive_ratio,
            "negative_ratio": agg.negative_ratio,
            "neutral_ratio": agg.neutral_ratio,
            "source_diversity": agg.source_diversity,
            "sentiment_confidence": agg.confidence,
        })

    sentiment_df = pd.DataFrame(sentiment_rows)
    merged = df.merge(sentiment_df, on="ticker", how="left")

    # Default fills for tickers that came back with nothing.
    merged["sentiment_score"] = merged["sentiment_score"].fillna(50.0)
    merged["article_count"] = merged["article_count"].fillna(0).astype(int)

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
    df["final_score"] = compute_composite_scores(df, config.composite)
    df = flag_rows(
        df,
        config.composite,
        low_sentiment_confidence_threshold=config.sentiment.low_confidence_threshold,
        weak_return_threshold=config.prices.weak_return_threshold,
    )
    ranked = rank_candidates(df, top_n=config.run.top_n)

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

    rate_limiter = RateLimiter(config.rate_limits.get("yfinance", 0.4))

    # Providers
    Fund = get_fundamentals_provider(config.providers.fundamentals)
    Price = get_prices_provider(config.providers.prices)
    News = get_news_provider(config.providers.news)

    fund_provider = Fund(
        cache=_namespaced_cache(cache, "fundamentals", config),
        rate_limiter=rate_limiter,
    )
    price_provider = Price(
        cache=_namespaced_cache(cache, "prices", config),
        rate_limiter=rate_limiter,
    )
    news_provider = News(
        cache=_namespaced_cache(cache, "news", config),
        rate_limiter=rate_limiter,
    )

    try:
        sentiment_model = get_sentiment_model(config.sentiment.model)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Sentiment model %s failed to load (%s); falling back to vader.",
            config.sentiment.model, exc,
        )
        sentiment_model = get_sentiment_model("vader")

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

    after_fund_df, _funds, s3 = _stage3_fundamentals(
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

    md = format_top_candidates_markdown(ranked, top_n=config.run.top_n)
    md_path.write_text(md, encoding="utf-8")

    total_runtime = time.perf_counter() - pipeline_started

    summary = _build_summary(
        ranked, config, mode=mode, sample_limit=sample_limit,
        stage_stats=stage_stats, exchange_breakdown=exchange_breakdown,
        total_runtime=total_runtime,
    )
    write_json(json_path, summary)

    _write_universe_summary(config, mode, exchange_breakdown, stage_stats)

    logger.info("CSV written  : %s", csv_path)
    logger.info("Markdown     : %s", md_path)
    logger.info("JSON summary : %s", json_path)
    logger.info("Total runtime: %.1fs", total_runtime)
    return 0


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
    examples: List[Dict[str, Any]] = []
    for s in stage_stats:
        if s.provider_failures:
            by_stage[s.name] = {
                "count": s.provider_failures,
                "reasons": dict(s.failure_reasons),
            }
            total += s.provider_failures
            for k, v in s.failure_reasons.items():
                total_reasons[k] = total_reasons.get(k, 0) + v
            for f in s.failures:
                examples.append({"stage": s.name, **f})
    return {
        "total": total,
        "by_reason": total_reasons,
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
        candidates.append({
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
            "sentiment_article_count": int(row.get("article_count", 0) or 0),
            "sentiment_positive_ratio": _safe_num(row.get("positive_ratio")),
            "sentiment_negative_ratio": _safe_num(row.get("negative_ratio")),
            "sentiment_confidence": _safe_num(row.get("sentiment_confidence")),
            "sentiment_source_diversity": int(row.get("source_diversity", 0) or 0),
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
            "reason": row.get("reason"),
            "warning_flags": list(row.get("flags") or []),
            "missing_fields": list(row.get("missing_fields") or []),
            "missing_metric_count": int(row.get("missing_metric_count", 0) or 0),
        })
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "mode": mode,
        "sample_limit": sample_limit,
        "top_n": int(config.run.top_n),
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
