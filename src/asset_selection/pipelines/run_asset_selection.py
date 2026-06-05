"""End-to-end asset-selection pipeline.

Stages:
    1. Load config + configure logging.
    2. Build (or load) the universe.
    3. For each ticker (capped by --limit): fetch fundamentals + prices + news.
    4. Score sentiment per article -> aggregate per ticker.
    5. Score fundamentals across the cross-section.
    6. Compute risk penalty + composite score.
    7. Rank, flag, and write outputs (CSV / Markdown / JSON).

Run it with:
    python -m asset_selection.pipelines.run_asset_selection \
        --config configs/default_config.yaml --limit 50 --top 20
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

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
from ..universe import build_universe, save_universe
from ..utils.cache import Cache
from ..utils.io import ensure_dir, write_csv, write_json
from ..utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="asset-selection",
        description="Rank U.S.-listed common stocks using free fundamentals + sentiment.",
    )
    p.add_argument(
        "--config", default="configs/default_config.yaml",
        help="Path to YAML config (default: configs/default_config.yaml)."
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of tickers processed."
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="Top-N tickers to include in the Markdown report."
    )
    p.add_argument(
        "--tickers", nargs="*", default=None,
        help="Run only on these tickers (skips universe build)."
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


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config)

    # CLI overrides
    if args.no_cache:
        config.cache.enabled = False
    if args.top is not None:
        config.run.top_n = args.top
    if args.limit is not None:
        config.run.max_tickers = args.limit
    if args.output_dir:
        config.run.output_dir = args.output_dir

    configure_logging(
        level=args.log_level or config.logging.level,
        log_to_file=config.logging.log_to_file,
        file=config.logging.file,
    )

    logger.info("Asset selection pipeline started at %s", datetime.utcnow().isoformat())
    logger.info(
        "Providers: fundamentals=%s prices=%s news=%s | sentiment=%s",
        config.providers.fundamentals,
        config.providers.prices,
        config.providers.news,
        config.sentiment.model,
    )

    cache = Cache(directory=config.cache.dir, enabled=config.cache.enabled)
    if args.refresh_cache and cache.enabled:
        removed = cache.invalidate()
        logger.info("Cleared cache: %d entries removed.", removed)

    rate_limiter = RateLimiter(config.rate_limits.get("yfinance", 0.4))

    # ------------------------------------------------------------------
    # Universe
    # ------------------------------------------------------------------
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers if t.strip()]
        universe_df = pd.DataFrame({"ticker": tickers, "company_name": tickers,
                                    "exchange": None, "asset_type": "common",
                                    "is_etf": False, "is_test_issue": False,
                                    "source": "cli"})
        logger.info("Running on %d user-supplied tickers.", len(universe_df))
    else:
        universe_df = build_universe(config, cache=cache, rate_limiter=rate_limiter)
        universe_path = Path(config.run.processed_dir) / "universe.csv"
        save_universe(universe_df, str(universe_path))

    if universe_df.empty:
        logger.error("Universe is empty. Exiting.")
        return 1

    if config.run.max_tickers and len(universe_df) > config.run.max_tickers:
        universe_df = universe_df.head(config.run.max_tickers).copy()
        logger.info("Capped universe to %d tickers.", config.run.max_tickers)

    tickers = universe_df["ticker"].tolist()

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------
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
        logger.warning("Sentiment model %s failed to load (%s); falling back to vader.",
                       config.sentiment.model, exc)
        sentiment_model = get_sentiment_model("vader")

    # ------------------------------------------------------------------
    # Per-ticker data fetch
    # ------------------------------------------------------------------
    fundamentals_records: List[Fundamentals] = []
    price_records: dict = {}
    news_records: dict = {}

    iterator = _progress(tickers, desc="Fetching tickers")
    for ticker in iterator:
        try:
            f = fund_provider.fetch(ticker)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Fundamentals fetch failed for %s: %s", ticker, exc)
            f = Fundamentals(ticker=ticker, source=config.providers.fundamentals)
        try:
            p = price_provider.fetch(ticker, lookback_days=config.prices.lookback_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Price fetch failed for %s: %s", ticker, exc)
            p = PriceSnapshot(ticker=ticker, lookback_days=config.prices.lookback_days,
                              source=config.providers.prices)
        try:
            n = news_provider.fetch(ticker, max_age_days=config.sentiment.max_age_days)
        except Exception as exc:  # noqa: BLE001
            logger.warning("News fetch failed for %s: %s", ticker, exc)
            n = []

        fundamentals_records.append(f)
        price_records[ticker] = p
        news_records[ticker] = n

    # ------------------------------------------------------------------
    # Score
    # ------------------------------------------------------------------
    logger.info("Scoring %d tickers...", len(fundamentals_records))

    fund_scores = score_fundamentals(fundamentals_records, config.scoring)

    sentiment_rows = []
    for ticker, articles in news_records.items():
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

    # Build the per-ticker results table
    fundamentals_meta = pd.DataFrame(
        [{
            "ticker": f.ticker,
            "company_name": f.company_name,
            "sector": f.sector,
            "industry": f.industry,
            "market_cap": f.market_cap,
            "missing_fields": f.missing_fields,
        } for f in fundamentals_records]
    )

    price_rows = pd.DataFrame([
        {
            "ticker": t,
            "last_close": p.last_close,
            "avg_daily_volume": p.avg_daily_volume,
            "avg_dollar_volume": p.avg_dollar_volume,
            "return_pct": p.return_pct,
            "volatility_pct": p.volatility_pct,
        }
        for t, p in price_records.items()
    ])

    results = fundamentals_meta.merge(price_rows, on="ticker", how="left")
    if not fund_scores.empty:
        results = results.merge(fund_scores, on="ticker", how="left")
    if not sentiment_df.empty:
        results = results.merge(sentiment_df, on="ticker", how="left")

    # Default fills where data is genuinely absent
    if "sentiment_score" in results.columns:
        results["sentiment_score"] = results["sentiment_score"].fillna(50.0)
    if "article_count" in results.columns:
        results["article_count"] = results["article_count"].fillna(0).astype(int)
    for col in ("fundamentals_score", "growth_score", "quality_score",
                "valuation_score", "balance_sheet_score", "cash_flow_score"):
        if col in results.columns:
            results[col] = results[col].fillna(50.0)
    if "missing_metric_count" in results.columns:
        results["missing_metric_count"] = results["missing_metric_count"].fillna(0).astype(int)

    # Risk penalty + composite
    results["risk_penalty"] = compute_risk_penalty(results, config.prices)
    results["final_score"] = compute_composite_scores(results, config.composite)
    results = flag_rows(
        results,
        config.composite,
        low_sentiment_confidence_threshold=config.sentiment.low_confidence_threshold,
        weak_return_threshold=config.prices.weak_return_threshold,
    )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    ranked = rank_candidates(results, top_n=config.run.top_n)

    processed_dir = ensure_dir(config.run.processed_dir)
    output_dir = ensure_dir(config.run.output_dir)

    csv_path = processed_dir / "asset_selection_results.csv"
    md_path = output_dir / "top_candidates.md"
    json_path = output_dir / "asset_selection_summary.json"

    write_csv(csv_path, ranked)

    md = format_top_candidates_markdown(ranked, top_n=config.run.top_n)
    md_path.write_text(md, encoding="utf-8")

    summary = _build_summary(ranked, config)
    write_json(json_path, summary)

    logger.info("CSV written  : %s", csv_path)
    logger.info("Markdown     : %s", md_path)
    logger.info("JSON summary : %s", json_path)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _namespaced_cache(cache: Cache, namespace: str, config: AppConfig) -> Cache:
    """Return a cache view with the right TTL for ``namespace``.

    Cache is a single on-disk store; we override the default TTL per provider
    by handing the provider a Cache instance whose ``default_ttl`` matches the
    config entry.
    """
    ttl = config.cache.ttl_seconds.get(namespace, cache.default_ttl)
    return Cache(directory=str(cache.directory), enabled=cache.enabled, default_ttl=ttl)


def _progress(items: Iterable, desc: str):
    try:
        from tqdm import tqdm

        return tqdm(list(items), desc=desc)
    except Exception:  # noqa: BLE001
        return items


def _build_summary(ranked: pd.DataFrame, config: AppConfig) -> dict:
    if ranked.empty:
        return {"generated_at": datetime.utcnow().isoformat() + "Z", "candidates": []}

    top = ranked.head(config.run.top_n).copy()
    candidates = []
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
            "risk_penalty": _safe_num(row.get("risk_penalty")),
            "final_score": _safe_num(row.get("final_score")),
            "reason": row.get("reason"),
            "warning_flags": list(row.get("flags") or []),
            "missing_fields": list(row.get("missing_fields") or []),
        })
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "universe_size": int(len(ranked)),
        "top_n": int(config.run.top_n),
        "providers": {
            "fundamentals": config.providers.fundamentals,
            "prices": config.providers.prices,
            "news": config.providers.news,
        },
        "sentiment_model": config.sentiment.model,
        "weights": config.composite.weights,
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
