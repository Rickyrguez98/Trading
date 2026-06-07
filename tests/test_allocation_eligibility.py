"""Allocation eligibility + reporting-consistency coverage.

These tests pin the Phase-2 contract: the research ranking keeps every scored
candidate (speculative and watchlist names included, labeled, never hidden),
while a separate *allocation shortlist* is gated by risk / data-quality /
sentiment thresholds. They also cover the confidence-adjusted sentiment, the
stale-news damping, the consistent provider-provenance block, and the JSON
count-of-record fields.
"""
from __future__ import annotations

import pandas as pd

from asset_selection.config import load_config
from asset_selection.scoring.allocation_eligibility import (
    allocation_field_summary,
    compute_allocation_fields,
)
from asset_selection.scoring.composite_score import (
    compute_composite_scores,
    compute_effective_confidence,
    compute_effective_sentiment,
    flag_rows,
)
from asset_selection.scoring.ranking import (
    format_top_candidates_markdown,
    rank_candidates,
)
from asset_selection.validation import (
    build_provider_diagnostics,
    build_provider_report,
    render_provider_provenance_note,
    write_provider_diagnostics,
)

CFG = load_config("configs/default_config.yaml")

# Columns compute_allocation_fields reads. Anything omitted defaults sanely.
_BASE = dict(
    flags=[], article_count=10, sentiment_confidence=0.7, fresh_ratio=0.9,
    missing_metric_count=0, market_cap=5e10,
)


def _row(ticker, bucket, **over):
    row = dict(_BASE)
    row.update(ticker=ticker, company_name=f"{ticker} Co", selection_bucket=bucket)
    row.update(over)
    return row


def _alloc(rows):
    """Build a ranked frame and attach allocation fields with the default config."""
    df = pd.DataFrame(rows)
    if "rank" not in df.columns:
        df = df.sort_values("final_score", ascending=False).reset_index(drop=True)
        df.insert(0, "rank", df.index + 1)
    return compute_allocation_fields(
        df,
        allocation_cfg=CFG.allocation,
        risk_controls=CFG.risk_controls,
        sentiment_cfg=CFG.sentiment,
    )


def _by_ticker(df, ticker):
    return df.loc[df["ticker"] == ticker].iloc[0]


# ---------------------------------------------------------------------------
# 1. Speculative / watchlist are never allocation-eligible by default.
# ---------------------------------------------------------------------------

def test_speculative_not_eligible_by_default():
    out = _alloc([
        _row("SPEC", "speculative_candidate", final_score=60.0, volatility_pct=1.0,
             risk_penalty=30.0, return_pct=0.80,
             flags=["HIGH_VOLATILITY", "SPECULATIVE_MOMENTUM"]),
    ])
    row = _by_ticker(out, "SPEC")
    assert bool(row["eligible_for_allocation"]) is False
    assert "speculative_candidate" in row["allocation_exclusion_reasons"]
    assert row["candidate_role"] == "speculative_research_only"
    assert row["recommended_next_step"] == "exclude_from_allocation_by_default"


def test_watchlist_not_eligible_by_default():
    out = _alloc([
        _row("WATCH", "watchlist_only", final_score=40.0, volatility_pct=0.40,
             risk_penalty=8.0, return_pct=-0.18, sentiment_confidence=0.2,
             fresh_ratio=0.1, missing_metric_count=5,
             flags=["WEAK_PRICE_TREND", "THIN_FUNDAMENTALS"]),
    ])
    row = _by_ticker(out, "WATCH")
    assert bool(row["eligible_for_allocation"]) is False
    assert "watchlist_only" in row["allocation_exclusion_reasons"]
    assert row["candidate_role"] == "watchlist_only"
    # The watchlist is a manual-review queue, not a hard default-exclude.
    assert row["recommended_next_step"] == "needs_manual_review"
    assert row["watchlist_rank"] == 1


def test_speculative_and_watchlist_allowed_via_config_override():
    cfg = load_config("configs/default_config.yaml")
    cfg.allocation.allow_speculative_for_allocation = True
    cfg.allocation.allow_watchlist_for_allocation = True
    df = pd.DataFrame([
        # Clean apart from the bucket, so only the bucket gate could block it.
        _row("SPEC", "speculative_candidate", final_score=60.0, volatility_pct=0.30,
             risk_penalty=5.0, return_pct=0.10),
        _row("WATCH", "watchlist_only", final_score=55.0, volatility_pct=0.30,
             risk_penalty=5.0, return_pct=0.10),
    ])
    df.insert(0, "rank", [1, 2])
    out = compute_allocation_fields(
        df, allocation_cfg=cfg.allocation, risk_controls=cfg.risk_controls,
        sentiment_cfg=cfg.sentiment,
    )
    # With the override + otherwise-clean rows, the bucket no longer blocks them.
    assert bool(_by_ticker(out, "SPEC")["eligible_for_allocation"]) is True
    assert bool(_by_ticker(out, "WATCH")["eligible_for_allocation"]) is True


# ---------------------------------------------------------------------------
# 2. Core CAN be eligible.
# ---------------------------------------------------------------------------

def test_core_candidate_can_be_eligible():
    out = _alloc([
        _row("CORE", "high_quality_core_candidate", final_score=72.0,
             volatility_pct=0.30, risk_penalty=5.0, return_pct=0.12),
    ])
    row = _by_ticker(out, "CORE")
    assert bool(row["eligible_for_allocation"]) is True
    assert row["allocation_exclusion_reasons"] == []
    assert row["candidate_role"] == "core_candidate"
    assert row["recommended_next_step"] == "eligible_for_portfolio_optimizer"
    assert row["risk_bucket"] == "low_risk"
    # adjusted score equals final_score when there are no penalties.
    assert abs(float(row["allocation_adjusted_score"]) - 72.0) < 1e-6


# ---------------------------------------------------------------------------
# 3. Growth eligibility is threshold-gated (vol cap is stricter for allocation).
# ---------------------------------------------------------------------------

def test_growth_eligibility_is_threshold_gated():
    # max_allocation_volatility defaults to 0.50; the research HIGH_VOLATILITY
    # ceiling is 0.80, so a growth name at 0.65 stays in research but fails the
    # stricter allocation gate, while the same name at 0.50 clears it.
    out = _alloc([
        _row("GLOW", "growth_candidate", final_score=62.0, volatility_pct=0.50,
             risk_penalty=10.0, return_pct=0.15, missing_metric_count=1),
        _row("GHIGH", "growth_candidate", final_score=62.0, volatility_pct=0.65,
             risk_penalty=10.0, return_pct=0.15, missing_metric_count=1),
    ])
    glow = _by_ticker(out, "GLOW")
    ghigh = _by_ticker(out, "GHIGH")
    assert bool(glow["eligible_for_allocation"]) is True
    assert bool(ghigh["eligible_for_allocation"]) is False
    assert "high_volatility" in ghigh["allocation_exclusion_reasons"]
    assert ghigh["recommended_next_step"] == "risk_review_needed"
    assert glow["candidate_role"] == "satellite_growth_candidate"


# ---------------------------------------------------------------------------
# 6. High volatility stays in research but is excluded from allocation.
# ---------------------------------------------------------------------------

def test_high_volatility_stays_in_research_but_excluded_from_allocation():
    out = _alloc([
        _row("CORE", "high_quality_core_candidate", final_score=72.0,
             volatility_pct=0.30, risk_penalty=5.0, return_pct=0.12),
        _row("VOL", "speculative_candidate", final_score=68.0, volatility_pct=0.95,
             risk_penalty=30.0, return_pct=0.40, flags=["HIGH_VOLATILITY"]),
    ])
    # Still in the research ranking (not dropped).
    assert set(out["ticker"]) == {"CORE", "VOL"}
    vol = _by_ticker(out, "VOL")
    assert bool(vol["eligible_for_allocation"]) is False
    assert vol["risk_bucket"] == "high_risk"
    # The report keeps it visible in section B but out of the shortlist (A).
    md = format_top_candidates_markdown(out, top_n=10)
    assert "VOL" in md  # research ranking
    shortlist = md.split("## B. Research ranking")[0]
    assert "VOL" not in shortlist  # not in the portfolio-eligible shortlist


def test_risk_bucket_is_flag_aware_below_volatility_ceiling():
    # Speculative-momentum with sub-ceiling headline vol must still read high_risk.
    out = _alloc([
        _row("MOM", "speculative_candidate", final_score=58.0, volatility_pct=0.62,
             risk_penalty=10.0, return_pct=0.70, flags=["SPECULATIVE_MOMENTUM"]),
    ])
    assert _by_ticker(out, "MOM")["risk_bucket"] == "high_risk"


# ---------------------------------------------------------------------------
# 4. Stale news reduces effective sentiment (and thus composite impact).
# ---------------------------------------------------------------------------

def test_stale_news_reduces_effective_sentiment():
    cfg = CFG.sentiment
    df = pd.DataFrame([
        dict(ticker="FRESH", sentiment_score=85.0, sentiment_confidence=0.8,
             fresh_ratio=0.90, article_count=10),
        dict(ticker="STALE", sentiment_score=85.0, sentiment_confidence=0.8,
             fresh_ratio=0.10, article_count=10),
    ])
    df["effective_sentiment_confidence"] = compute_effective_confidence(df, cfg)
    df["effective_sentiment_score"] = compute_effective_sentiment(df, cfg)
    fresh = df.loc[df["ticker"] == "FRESH"].iloc[0]
    stale = df.loc[df["ticker"] == "STALE"].iloc[0]
    # Stale news damps the effective confidence and pulls sentiment toward neutral.
    assert stale["effective_sentiment_confidence"] < fresh["effective_sentiment_confidence"]
    assert stale["effective_sentiment_score"] < fresh["effective_sentiment_score"]
    neutral = cfg.neutral_sentiment_score
    assert abs(stale["effective_sentiment_score"] - neutral) < abs(
        fresh["effective_sentiment_score"] - neutral
    )
    # ...and that flows through to a lower composite for the stale name.
    for col in ("fundamentals_score", "growth_score", "quality_score",
                "valuation_score"):
        df[col] = 60.0
    df["risk_penalty"] = 0.0
    composite = compute_composite_scores(
        df, CFG.composite, sentiment_column="effective_sentiment_score"
    )
    assert composite.loc[df["ticker"] == "STALE"].iloc[0] < composite.loc[
        df["ticker"] == "FRESH"
    ].iloc[0]


def test_low_confidence_pulls_sentiment_toward_neutral():
    # The GEN case: strong raw sentiment but thin/low-confidence feed must not be
    # rewarded as if it were a confident signal.
    df = pd.DataFrame([
        dict(ticker="HICONF", sentiment_score=85.0, sentiment_confidence=0.9,
             fresh_ratio=0.9, article_count=20),
        dict(ticker="LOCONF", sentiment_score=85.0, sentiment_confidence=0.15,
             fresh_ratio=0.9, article_count=2),
    ])
    df["effective_sentiment_confidence"] = compute_effective_confidence(df, CFG.sentiment)
    df["effective_sentiment_score"] = compute_effective_sentiment(df, CFG.sentiment)
    hi = df.loc[df["ticker"] == "HICONF"].iloc[0]["effective_sentiment_score"]
    lo = df.loc[df["ticker"] == "LOCONF"].iloc[0]["effective_sentiment_score"]
    assert lo < hi
    assert lo < 60.0  # pulled well back toward the neutral 50


def test_confidence_adjustment_is_config_reversible():
    cfg = load_config("configs/default_config.yaml")
    cfg.sentiment.use_confidence_adjusted_sentiment = False
    df = pd.DataFrame([
        dict(ticker="X", sentiment_score=85.0, sentiment_confidence=0.1,
             fresh_ratio=0.9, article_count=2),
    ])
    eff = compute_effective_sentiment(df, cfg.sentiment)
    assert abs(eff.iloc[0] - 85.0) < 1e-6  # pass-through when disabled


# ---------------------------------------------------------------------------
# 5. Low confidence / stale flags from flag_rows.
# ---------------------------------------------------------------------------

def test_low_confidence_adds_flag():
    df = pd.DataFrame([
        dict(ticker="LO", fundamentals_score=60, sentiment_score=55, risk_penalty=0,
             article_count=2, sentiment_confidence=0.2, fresh_ratio=0.9,
             source_diversity=3, market_cap=1e10, missing_metric_count=0),
        dict(ticker="HI", fundamentals_score=60, sentiment_score=55, risk_penalty=0,
             article_count=12, sentiment_confidence=0.8, fresh_ratio=0.9,
             source_diversity=4, market_cap=1e10, missing_metric_count=0),
    ])
    out = flag_rows(
        df, CFG.composite,
        low_sentiment_confidence_threshold=CFG.sentiment.low_confidence_threshold,
        weak_return_threshold=CFG.prices.weak_return_threshold,
        risk_controls=CFG.risk_controls,
        stale_news_fresh_ratio_threshold=CFG.sentiment.stale_news_fresh_ratio_threshold,
        very_stale_news_fresh_ratio_threshold=CFG.sentiment.very_stale_news_fresh_ratio_threshold,
        low_source_diversity_threshold=CFG.sentiment.low_source_diversity_threshold,
    )
    lo_flags = out.loc[out["ticker"] == "LO"].iloc[0]["flags"]
    hi_flags = out.loc[out["ticker"] == "HI"].iloc[0]["flags"]
    assert "LOW_SENTIMENT_CONFIDENCE" in lo_flags
    assert "LOW_SENTIMENT_CONFIDENCE" not in hi_flags


def test_stale_and_diversity_flags_fire():
    df = pd.DataFrame([
        dict(ticker="VS", fundamentals_score=55, sentiment_score=55, risk_penalty=0,
             article_count=4, sentiment_confidence=0.6, fresh_ratio=0.10,
             source_diversity=1, market_cap=1e10, missing_metric_count=0),
    ])
    out = flag_rows(
        df, CFG.composite,
        low_sentiment_confidence_threshold=CFG.sentiment.low_confidence_threshold,
        weak_return_threshold=CFG.prices.weak_return_threshold,
        risk_controls=CFG.risk_controls,
        stale_news_fresh_ratio_threshold=CFG.sentiment.stale_news_fresh_ratio_threshold,
        very_stale_news_fresh_ratio_threshold=CFG.sentiment.very_stale_news_fresh_ratio_threshold,
        low_source_diversity_threshold=CFG.sentiment.low_source_diversity_threshold,
    )
    flags = out.iloc[0]["flags"]
    assert "VERY_STALE_NEWS" in flags
    assert "STALE_NEWS" not in flags  # very-stale supersedes stale
    assert "LOW_SOURCE_DIVERSITY" in flags


# ---------------------------------------------------------------------------
# 7. Provider reporting is consistent (and unwrapped providers aren't zeroed).
# ---------------------------------------------------------------------------

def test_provider_report_consistent_and_no_misleading_zeros(tmp_path):
    configured = {"prices": "yfinance", "fundamentals": "yfinance", "news": "yfinance"}
    fallback_usage = {
        "prices": {"chain": ["yfinance", "stooq"], "wrapped": True,
                   "primary": 40, "fallback": 8, "stale_cache": 0, "unavailable": 2,
                   "by_provider": {"yfinance": 40, "stooq": 8}},
        "fundamentals": {"chain": ["yfinance"], "wrapped": False},
        "news": {"chain": ["yfinance"], "wrapped": False},
    }
    cache_usage = {"prices": {"live": 45, "fresh_cache": 5},
                   "fundamentals": {"live": 48, "unavailable": 2}}
    report = build_provider_report(
        configured_providers=configured, fallback_usage=fallback_usage,
        cache_usage=cache_usage,
    )
    # Same keys everywhere.
    assert set(report) == {
        "configured_providers", "provider_chain_by_data_type",
        "actual_provider_usage", "cache_usage_by_stage",
    }
    assert report["provider_chain_by_data_type"]["prices"] == ["yfinance", "stooq"]
    # The unwrapped provider is marked not-instrumented and carries NO zeroed
    # primary/fallback counters (the misleading-zero fix).
    fund = report["actual_provider_usage"]["fundamentals"]
    assert fund["instrumented"] is False
    assert "counters" not in fund
    assert fund["by_source"] == {"live": 48, "unavailable": 2}
    # The wrapped provider keeps its real counters.
    assert report["actual_provider_usage"]["prices"]["counters"]["fallback"] == 8

    # The diagnostics, summary block, and footer all name the same chain.
    status = {"run_status": "VALID", "ranking_validity": "VALID_RANKING",
              "is_trusted": True, "invalid_ranking_reasons": [], "warnings": [],
              "recommendations_for_next_run": []}
    diag = build_provider_diagnostics(
        status=status, coverage={}, health_report=None, provider_failures={},
        fallback_usage=fallback_usage, cache_usage=cache_usage, providers=configured,
    )
    assert diag["provider_report"]["provider_chain_by_data_type"] == \
        report["provider_chain_by_data_type"]
    _, md_path = write_provider_diagnostics(diag, tmp_path)
    md = md_path.read_text()
    note = render_provider_provenance_note(report)
    # Markdown shows the chain and uses "—" (not 0) for the unwrapped rows.
    assert "yfinance → stooq" in md
    assert "yfinance → stooq" in note
    assert "| fundamentals | no | — | — | — | — |" in md


# ---------------------------------------------------------------------------
# 8 & 9. JSON counts + allocation fields in CSV / JSON / Markdown.
# ---------------------------------------------------------------------------

_ALLOC_FIELDS = {
    "eligible_for_allocation", "allocation_adjusted_score",
    "allocation_exclusion_reasons", "exclusion_reason_from_allocation",
    "risk_bucket", "sentiment_quality_bucket", "data_quality_bucket",
    "candidate_role", "recommended_next_step", "watchlist_rank",
}


def _mixed_ranked():
    rows = [
        _row("CORE", "high_quality_core_candidate", final_score=72.0,
             volatility_pct=0.30, risk_penalty=5.0, return_pct=0.12),
        _row("GROW", "growth_candidate", final_score=62.0, volatility_pct=0.50,
             risk_penalty=10.0, return_pct=0.15, missing_metric_count=1),
        _row("SPEC", "speculative_candidate", final_score=58.0, volatility_pct=1.0,
             risk_penalty=30.0, return_pct=0.80, flags=["HIGH_VOLATILITY"]),
        _row("WATCH", "watchlist_only", final_score=40.0, volatility_pct=0.40,
             risk_penalty=8.0, return_pct=-0.18, missing_metric_count=5,
             flags=["WEAK_PRICE_TREND", "THIN_FUNDAMENTALS"]),
    ]
    return _alloc(rows)


def test_allocation_fields_present_in_dataframe_for_csv():
    out = _mixed_ranked()
    # The CSV is a direct dump of this frame, so the columns must be present.
    assert _ALLOC_FIELDS.issubset(set(out.columns))


def test_allocation_field_summary_is_json_safe():
    out = _mixed_ranked()
    summary = allocation_field_summary(_by_ticker(out, "WATCH"))
    assert set(summary) == _ALLOC_FIELDS
    assert summary["eligible_for_allocation"] is False
    assert summary["watchlist_rank"] == 1
    assert summary["candidate_role"] == "watchlist_only"
    # eligible name reports a null watchlist_rank.
    assert allocation_field_summary(_by_ticker(out, "CORE"))["watchlist_rank"] is None


def test_summary_reports_ranked_vs_reported_counts():
    from asset_selection.pipelines.run_asset_selection import _build_summary

    cfg = load_config("configs/default_config.yaml")
    cfg.run.top_n = 2
    out = _mixed_ranked()
    summary = _build_summary(
        out, cfg, mode="custom", sample_limit=None, stage_stats=[],
        exchange_breakdown={}, total_runtime=0.0,
    )
    assert summary["ranked_candidate_count"] == 4
    assert summary["reported_candidate_count"] == 2
    assert len(summary["candidates"]) == 2
    assert summary["allocation_eligible_count"] == 2  # CORE + GROW
    # Every reported candidate carries the allocation fields (JSON parity).
    for cand in summary["candidates"]:
        assert _ALLOC_FIELDS.issubset(set(cand))


def test_markdown_has_shortlist_and_allocation_columns():
    md = format_top_candidates_markdown(_mixed_ranked(), top_n=10)
    assert "## A. Portfolio-eligible shortlist" in md
    assert "## B. Research ranking" in md
    assert "## C. Speculative candidates" in md
    assert "## D. Watchlist-only candidates" in md
    assert "## E. Excluded-from-allocation reasons" in md
    assert "allocation_adjusted_score" in md
    assert "eligible_for_allocation" in md
