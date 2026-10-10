"""Risk-control flags and selection_bucket labeling.

Reproduces rows from the audited full-universe run to lock in the intended
behaviour: volatile names are LABELED, not removed.
"""
from __future__ import annotations

import pandas as pd

from asset_selection.config import CompositeConfig, RiskControlsConfig
from asset_selection.scoring.composite_score import flag_rows


def _frame(rows):
    return pd.DataFrame(rows)


def _flag(df):
    return flag_rows(
        df,
        CompositeConfig(),
        low_sentiment_confidence_threshold=0.3,
        weak_return_threshold=-0.10,
        risk_controls=RiskControlsConfig(),
    )


def test_high_volatility_flag_and_speculative_bucket():
    # ONDS-like: +110% annualized vol.
    df = _frame([{
        "ticker": "ONDS", "fundamentals_score": 60.6, "sentiment_score": 81.3,
        "return_pct": 0.061, "volatility_pct": 1.102, "risk_penalty": 16.8,
        "article_count": 10, "sentiment_confidence": 1.0, "missing_metric_count": 0,
        "market_cap": 5.32e9,
    }])
    out = _flag(df)
    flags = out.loc[0, "flags"]
    assert "HIGH_VOLATILITY" in flags
    assert out.loc[0, "selection_bucket"] == "speculative_candidate"
    # The volatile name is still present -- labeled, not dropped.
    assert set(out["ticker"]) == {"ONDS"}


def test_speculative_momentum_flag():
    # CRDO-like: +88% return on +97% vol.
    df = _frame([{
        "ticker": "CRDO", "fundamentals_score": 61.6, "sentiment_score": 79.5,
        "return_pct": 0.884, "volatility_pct": 0.974, "risk_penalty": 15.1,
        "article_count": 10, "sentiment_confidence": 1.0, "missing_metric_count": 0,
        "market_cap": 3.8e10,
    }])
    out = _flag(df)
    flags = out.loc[0, "flags"]
    assert "SPECULATIVE_MOMENTUM" in flags
    assert "HIGH_VOLATILITY" in flags
    assert out.loc[0, "selection_bucket"] == "speculative_candidate"


def test_negative_return_name_is_watchlist_not_core():
    # CDE-like: negative return, moderate vol -> WEAK_PRICE_TREND -> watchlist.
    df = _frame([{
        "ticker": "CDE", "fundamentals_score": 62.4, "sentiment_score": 70.9,
        "return_pct": -0.276, "volatility_pct": 0.738, "risk_penalty": 21.2,
        "article_count": 10, "sentiment_confidence": 1.0, "missing_metric_count": 0,
        "market_cap": 1.68e10,
    }])
    out = _flag(df)
    flags = out.loc[0, "flags"]
    assert "WEAK_PRICE_TREND" in flags
    assert out.loc[0, "selection_bucket"] == "watchlist_only"


def test_steady_compounder_is_high_quality_core():
    # NVDA-like: strong fundamentals, contained vol, low risk, positive return.
    df = _frame([{
        "ticker": "NVDA", "fundamentals_score": 62.0, "sentiment_score": 61.2,
        "return_pct": 0.155, "volatility_pct": 0.396, "risk_penalty": 1.5,
        "article_count": 10, "sentiment_confidence": 1.0, "missing_metric_count": 0,
        "market_cap": 4.97e12,
    }])
    out = _flag(df)
    assert out.loc[0, "selection_bucket"] == "high_quality_core_candidate"
    assert "HIGH_VOLATILITY" not in out.loc[0, "flags"]


def test_buckets_label_every_row_and_never_drop():
    df = _frame([
        {"ticker": "A", "fundamentals_score": 70, "volatility_pct": 0.30,
         "return_pct": 0.10, "risk_penalty": 2.0, "article_count": 10,
         "sentiment_confidence": 1.0, "missing_metric_count": 0, "market_cap": 1e11},
        {"ticker": "B", "fundamentals_score": 30, "volatility_pct": 1.50,
         "return_pct": 0.90, "risk_penalty": 60.0, "article_count": 10,
         "sentiment_confidence": 1.0, "missing_metric_count": 0, "market_cap": 1e9},
    ])
    out = _flag(df)
    # No row dropped; every row gets a non-empty bucket.
    assert len(out) == 2
    assert all(out["selection_bucket"].str.len() > 0)
