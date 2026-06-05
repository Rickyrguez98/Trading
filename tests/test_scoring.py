"""Composite scoring, risk penalty, and flag logic."""
from __future__ import annotations

import pandas as pd

from asset_selection.config import load_config
from asset_selection.scoring.composite_score import (
    compute_composite_scores,
    compute_risk_penalty,
    flag_rows,
)


def _row(**overrides):
    base = {
        "ticker": "TEST",
        "company_name": "Test Co",
        "fundamentals_score": 70.0,
        "growth_score": 60.0,
        "quality_score": 65.0,
        "valuation_score": 55.0,
        "balance_sheet_score": 70.0,
        "cash_flow_score": 60.0,
        "sentiment_score": 60.0,
        "article_count": 5,
        "market_cap": 5e9,
        "avg_dollar_volume": 1e8,
        "volatility_pct": 0.3,
        "missing_metric_count": 0,
    }
    base.update(overrides)
    return base


def test_composite_clipped_to_0_100():
    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(fundamentals_score=100, growth_score=100, quality_score=100,
             valuation_score=100, sentiment_score=100,
             missing_metric_count=0),
        _row(ticker="LOW", fundamentals_score=0, growth_score=0, quality_score=0,
             valuation_score=0, sentiment_score=0, market_cap=1e6,
             avg_dollar_volume=1e3, volatility_pct=2.0, missing_metric_count=10),
    ])
    df["risk_penalty"] = compute_risk_penalty(df, cfg.prices)
    out = compute_composite_scores(df, cfg.composite)
    assert (out >= 0).all() and (out <= 100).all()


def test_fundamentals_dominate_sentiment_at_default_weights():
    cfg = load_config("configs/default_config.yaml")
    a = _row(ticker="GOOD_FUND_BAD_NEWS", fundamentals_score=85,
             growth_score=80, quality_score=80, valuation_score=70,
             sentiment_score=20, article_count=10)
    b = _row(ticker="BAD_FUND_GOOD_NEWS", fundamentals_score=25,
             growth_score=20, quality_score=20, valuation_score=30,
             sentiment_score=90, article_count=10)
    df = pd.DataFrame([a, b])
    df["risk_penalty"] = 0.0
    out = compute_composite_scores(df, cfg.composite)
    df["final_score"] = out
    s = df.set_index("ticker")["final_score"]
    assert s["GOOD_FUND_BAD_NEWS"] > s["BAD_FUND_GOOD_NEWS"], (
        "Default weights must let fundamentals outweigh sentiment."
    )


def test_flag_speculative_hype_and_review():
    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(ticker="HYPE", fundamentals_score=30, sentiment_score=80, article_count=10),
        _row(ticker="REVIEW", fundamentals_score=80, sentiment_score=25, article_count=10),
        _row(ticker="QUIET", article_count=0, sentiment_score=50.0),
    ])
    df["risk_penalty"] = 0.0
    df["final_score"] = 50.0
    flagged = flag_rows(df, cfg.composite).set_index("ticker")
    assert "SPECULATIVE_HYPE" in flagged.loc["HYPE", "flags"]
    assert "STRONG_FUNDAMENTALS_BAD_SENTIMENT" in flagged.loc["REVIEW", "flags"]
    assert "NO_NEWS" in flagged.loc["QUIET", "flags"]


def test_risk_penalty_increases_with_low_liquidity_and_missing_data():
    cfg = load_config("configs/default_config.yaml")
    healthy = _row(ticker="HEALTHY", market_cap=5e10, avg_dollar_volume=5e8,
                   volatility_pct=0.25, missing_metric_count=0)
    fragile = _row(ticker="FRAGILE", market_cap=1e7, avg_dollar_volume=1e4,
                   volatility_pct=1.5, missing_metric_count=8)
    df = pd.DataFrame([healthy, fragile])
    pen = compute_risk_penalty(df, cfg.prices)
    assert pen.loc[1] > pen.loc[0]
