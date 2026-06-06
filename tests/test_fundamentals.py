"""Fundamentals scoring + missing-data handling."""
from __future__ import annotations

import pandas as pd

from asset_selection.config import load_config
from asset_selection.data_providers.base import Fundamentals
from asset_selection.fundamentals.fundamental_scoring import (
    fundamentals_dataframe,
    normalize_metrics,
    score_fundamentals,
)


def _records():
    # Build a cross-section of 5 mock tickers.
    return [
        Fundamentals(
            ticker="A",
            revenue_growth=0.30, earnings_growth=0.25, fcf_growth=0.20,
            operating_margin=0.30, net_margin=0.20, roe=0.35, roa=0.18,
            debt_to_equity=50.0, current_ratio=2.0,
            free_cash_flow_yield=0.05, operating_cash_flow_margin=0.25,
            pe_ratio=15.0, forward_pe=14.0, peg_ratio=1.0,
            price_to_sales=4.0, price_to_book=5.0, market_cap=1e11,
        ),
        Fundamentals(
            ticker="B",
            revenue_growth=0.05, earnings_growth=0.02, fcf_growth=0.01,
            operating_margin=0.10, net_margin=0.05, roe=0.08, roa=0.04,
            debt_to_equity=200.0, current_ratio=1.0,
            free_cash_flow_yield=0.01, operating_cash_flow_margin=0.07,
            pe_ratio=40.0, forward_pe=35.0, peg_ratio=3.0,
            price_to_sales=8.0, price_to_book=12.0, market_cap=5e9,
        ),
        Fundamentals(
            ticker="C",
            revenue_growth=-0.10, earnings_growth=-0.20, fcf_growth=-0.30,
            operating_margin=-0.05, net_margin=-0.10, roe=-0.10, roa=-0.05,
            debt_to_equity=400.0, current_ratio=0.5,
            free_cash_flow_yield=-0.02, operating_cash_flow_margin=-0.03,
            pe_ratio=None, forward_pe=None, peg_ratio=None,
            price_to_sales=15.0, price_to_book=20.0, market_cap=2e8,
        ),
        Fundamentals(
            ticker="D",
            revenue_growth=0.12, earnings_growth=0.10, fcf_growth=0.09,
            operating_margin=0.20, net_margin=0.12, roe=0.22, roa=0.10,
            debt_to_equity=80.0, current_ratio=1.5,
            free_cash_flow_yield=0.03, operating_cash_flow_margin=0.15,
            pe_ratio=20.0, forward_pe=18.0, peg_ratio=1.5,
            price_to_sales=5.0, price_to_book=6.0, market_cap=3e10,
        ),
        # Mostly missing -> should be penalized
        Fundamentals(ticker="E", market_cap=1e9),
    ]


def test_normalize_metrics_handles_missing():
    df = fundamentals_dataframe(_records())
    cfg = load_config("configs/default_config.yaml").scoring
    out = normalize_metrics(df, ["roe", "net_margin", "pe_ratio"], cfg)
    assert set(out.index) == {"A", "B", "C", "D", "E"}
    # E has all missing -> all NaN
    assert out.loc["E"].isna().all()
    # A (best on ROE/margins) should score higher than B and C on quality metrics
    assert out.loc["A", "roe"] > out.loc["B", "roe"]
    assert out.loc["A", "roe"] > out.loc["C", "roe"]


def test_valuation_inversion_means_cheaper_is_better():
    df = fundamentals_dataframe(_records())
    cfg = load_config("configs/default_config.yaml").scoring
    out = normalize_metrics(df, ["pe_ratio"], cfg)
    # A has pe=15 (cheap); B has pe=40 (expensive) -> A's score must beat B's.
    assert out.loc["A", "pe_ratio"] > out.loc["B", "pe_ratio"]


def test_score_fundamentals_penalizes_missing_data_ticker():
    cfg = load_config("configs/default_config.yaml").scoring
    scores = score_fundamentals(_records(), cfg)
    s = scores.set_index("ticker")
    # The ticker with no disclosed fundamentals must not rank highest.
    assert s.loc["E", "fundamentals_score"] <= s.loc["A", "fundamentals_score"]
    assert s.loc["E", "missing_metric_count"] >= 5


def test_score_fundamentals_orders_high_quality_first():
    cfg = load_config("configs/default_config.yaml").scoring
    scores = score_fundamentals(_records(), cfg)
    s = scores.set_index("ticker")
    # A (great growth + margins + cheap) should outscore C (negatives across the board).
    assert s.loc["A", "fundamentals_score"] > s.loc["C", "fundamentals_score"]


def test_score_fundamentals_adds_explainability_columns():
    cfg = load_config("configs/default_config.yaml").scoring
    scores = score_fundamentals(_records(), cfg)
    s = scores.set_index("ticker")
    for col in (
        "strongest_metric", "weakest_metric",
        "market_cap_available", "valuation_metrics_available",
    ):
        assert col in s.columns
    # A and D disclose market cap and all 5 valuation ratios.
    assert bool(s.loc["A", "market_cap_available"]) is True
    assert int(s.loc["A", "valuation_metrics_available"]) == 5
    # C is missing pe/forward_pe/peg -> fewer valuation ratios available.
    assert int(s.loc["C", "valuation_metrics_available"]) < 5
    # The fully-disclosed strong name should name a real strongest metric.
    assert isinstance(s.loc["A", "strongest_metric"], str)
    # The empty ticker E has no metrics -> strongest/weakest are None.
    assert s.loc["E", "strongest_metric"] is None
