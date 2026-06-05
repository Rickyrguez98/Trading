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


def test_negative_momentum_increases_risk_and_flags(tmp_path):
    """Closes test gap from the audit: price history must influence the score."""
    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(ticker="UP", return_pct=0.20),
        _row(ticker="FLAT", return_pct=0.0),
        _row(ticker="DOWN", return_pct=-0.30),
    ])
    df["risk_penalty"] = compute_risk_penalty(df, cfg.prices)
    flagged = flag_rows(
        df, cfg.composite,
        low_sentiment_confidence_threshold=cfg.sentiment.low_confidence_threshold,
        weak_return_threshold=cfg.prices.weak_return_threshold,
    ).set_index("ticker")

    assert flagged.loc["DOWN", "risk_penalty"] > flagged.loc["UP", "risk_penalty"]
    assert flagged.loc["DOWN", "risk_penalty"] > flagged.loc["FLAT", "risk_penalty"]
    assert "WEAK_PRICE_TREND" in flagged.loc["DOWN", "flags"]
    assert "WEAK_PRICE_TREND" not in flagged.loc["UP", "flags"]
    assert "WEAK_PRICE_TREND" not in flagged.loc["FLAT", "flags"]


def test_sentiment_difference_changes_final_ranking():
    """Identical fundamentals + price; only sentiment differs -> rank flips."""
    cfg = load_config("configs/default_config.yaml")
    a = _row(ticker="POS_NEWS", sentiment_score=90, article_count=10)
    b = _row(ticker="NEG_NEWS", sentiment_score=10, article_count=10)
    df = pd.DataFrame([a, b])
    df["risk_penalty"] = compute_risk_penalty(df, cfg.prices)
    df["final_score"] = compute_composite_scores(df, cfg.composite)
    s = df.set_index("ticker")["final_score"]
    assert s["POS_NEWS"] > s["NEG_NEWS"], (
        "Sentiment must influence final_score when all other inputs match."
    )


def test_low_sentiment_confidence_flag_fires():
    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(ticker="THIN", article_count=1, sentiment_confidence=0.1),
        _row(ticker="DEEP", article_count=8, sentiment_confidence=0.9),
    ])
    df["risk_penalty"] = 0.0
    flagged = flag_rows(
        df, cfg.composite,
        low_sentiment_confidence_threshold=cfg.sentiment.low_confidence_threshold,
        weak_return_threshold=cfg.prices.weak_return_threshold,
    ).set_index("ticker")
    assert "LOW_SENTIMENT_CONFIDENCE" in flagged.loc["THIN", "flags"]
    assert "LOW_SENTIMENT_CONFIDENCE" not in flagged.loc["DEEP", "flags"]


def test_top_driver_and_drag_are_emitted():
    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(ticker="MIXED", growth_score=90, quality_score=60,
             valuation_score=20, balance_sheet_score=50, cash_flow_score=55),
    ])
    df["risk_penalty"] = 0.0
    flagged = flag_rows(df, cfg.composite).iloc[0]
    assert flagged["top_driver_pillar"] == "growth"
    assert flagged["top_drag_pillar"] == "valuation"
    assert "top_driver=growth" in flagged["reason"]
    assert "top_drag=valuation" in flagged["reason"]


def test_ranking_explainable_every_top_row_has_reason_and_flag_list():
    from asset_selection.scoring.ranking import rank_candidates

    cfg = load_config("configs/default_config.yaml")
    df = pd.DataFrame([
        _row(ticker="A"), _row(ticker="B"), _row(ticker="C"),
    ])
    df["risk_penalty"] = compute_risk_penalty(df, cfg.prices)
    df["final_score"] = compute_composite_scores(df, cfg.composite)
    df = flag_rows(df, cfg.composite)
    ranked = rank_candidates(df, top_n=3)
    for _, row in ranked.iterrows():
        assert isinstance(row["flags"], list), "flags must be a list"
        assert isinstance(row["reason"], str) and row["reason"], "reason must be non-empty"
