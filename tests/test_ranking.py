"""Final ranking logic."""
from __future__ import annotations

import pandas as pd

from asset_selection.scoring.ranking import format_top_candidates_markdown, rank_candidates


def _df():
    return pd.DataFrame([
        {"ticker": "A", "company_name": "A Co", "sector": "Tech",
         "final_score": 70, "fundamentals_score": 70, "sentiment_score": 60,
         "growth_score": 65, "quality_score": 60, "valuation_score": 60,
         "risk_penalty": 5, "market_cap": 1e10, "avg_dollar_volume": 1e7,
         "volatility_pct": 0.3, "article_count": 5,
         "flags": [], "reason": "", "missing_fields": []},
        {"ticker": "B", "company_name": "B Co", "sector": "Health",
         "final_score": 85, "fundamentals_score": 80, "sentiment_score": 70,
         "growth_score": 75, "quality_score": 70, "valuation_score": 65,
         "risk_penalty": 0, "market_cap": 5e10, "avg_dollar_volume": 5e7,
         "volatility_pct": 0.2, "article_count": 10,
         "flags": [], "reason": "", "missing_fields": []},
        {"ticker": "C", "company_name": "C Co", "sector": "Energy",
         "final_score": 50, "fundamentals_score": 50, "sentiment_score": 50,
         "growth_score": 50, "quality_score": 50, "valuation_score": 50,
         "risk_penalty": 10, "market_cap": 1e9, "avg_dollar_volume": 1e6,
         "volatility_pct": 0.5, "article_count": 0,
         "flags": ["NO_NEWS"], "reason": "", "missing_fields": []},
    ])


def test_rank_orders_by_final_score_desc():
    ranked = rank_candidates(_df(), top_n=5)
    assert list(ranked["ticker"]) == ["B", "A", "C"]
    assert list(ranked["rank"]) == [1, 2, 3]


def test_rank_handles_empty():
    out = rank_candidates(pd.DataFrame(), top_n=5)
    assert out.empty


def test_markdown_report_includes_top_only_with_disclaimer():
    md = format_top_candidates_markdown(rank_candidates(_df(), top_n=2), top_n=2)
    assert "Asset Selection — Top Candidates" in md
    assert "Not financial advice" in md
    # B should be ranked above A; C is below the top-2 cut and must be absent.
    assert md.index("B Co") < md.index("A Co")
    assert "C Co" not in md
