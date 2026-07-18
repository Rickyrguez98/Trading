"""End-to-end (offline) check that comparison sentiment flows through the
pipeline: stage 4 -> stage 5 -> summary JSON -> validation -> markdown note.

The network is not used: a fake news provider feeds canned articles and FinBERT
is a deterministic mock injected into the runtime. This exercises the real
``_stage4_sentiment`` / ``_stage5_compose_and_rank`` / ``_build_summary`` /
``render_sentiment_model_note`` / ``validate_outputs`` code paths.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from asset_selection.config import load_config
from asset_selection.data_providers.base import NewsItem
from asset_selection.pipelines.run_asset_selection import (
    _build_summary,
    _stage4_sentiment,
    _stage5_compose_and_rank,
    render_sentiment_model_note,
)
from asset_selection.sentiment.comparison import SentimentRuntime
from asset_selection.sentiment.sentiment_model import SentimentModel, VaderSentimentModel
from asset_selection.validation import validate_outputs


class DummyModel(SentimentModel):
    def __init__(self, scores):
        self._scores = list(scores)
        self._idx = 0

    def score(self, text: str) -> float:  # noqa: ARG002
        v = self._scores[self._idx % len(self._scores)]
        self._idx += 1
        return v


class FakeNews:
    def __init__(self, by_ticker):
        self.by = by_ticker

    def fetch(self, ticker, max_age_days=30):  # noqa: ARG002
        return self.by.get(ticker, [])


def _arts(ticker, n):
    base = datetime.now(timezone.utc)
    return [
        NewsItem(
            ticker=ticker, headline=f"{ticker} story {i}", summary=f"body {i}",
            source=f"wire{i % 2}", url=f"https://e.com/{ticker}/{i}",
            published_at=base.isoformat(), retrieved_at=base.isoformat(),
        )
        for i in range(n)
    ]


def _base_frame():
    return pd.DataFrame([
        {
            "ticker": "AAA", "company_name": "Alpha Inc", "sector": "Tech",
            "industry": "Software", "market_cap": 5e10, "avg_dollar_volume": 2e8,
            "last_close": 100.0, "return_pct": 0.05, "volatility_pct": 0.25,
            "fundamentals_score": 78, "growth_score": 70, "quality_score": 72,
            "valuation_score": 60, "balance_sheet_score": 65, "cash_flow_score": 68,
            "missing_metric_count": 0, "market_cap_available": True,
            "valuation_metrics_available": 3, "missing_fields": [],
        },
        {
            "ticker": "BBB", "company_name": "Beta Corp", "sector": "Health",
            "industry": "Pharma", "market_cap": 2e10, "avg_dollar_volume": 9e7,
            "last_close": 40.0, "return_pct": -0.02, "volatility_pct": 0.35,
            "fundamentals_score": 66, "growth_score": 55, "quality_score": 60,
            "valuation_score": 58, "balance_sheet_score": 62, "cash_flow_score": 59,
            "missing_metric_count": 1, "market_cap_available": True,
            "valuation_metrics_available": 2, "missing_fields": [],
        },
    ])


def _comparison_runtime():
    # VADER real; FinBERT mocked strongly negative so AAA shows a big disagreement.
    return SentimentRuntime(
        vader_model=VaderSentimentModel(),
        finbert_model=DummyModel([-0.9]),
        comparison_mode=True,
        finbert_deps_present=True,
        finbert_loaded=True,
        finbert_attempted=True,
        model_name="comparison",
    )


def test_comparison_flows_through_pipeline_to_reports():
    config = load_config(None)
    config.sentiment.model = "comparison"
    config.sentiment.final_sentiment_source = "vader"
    config.sentiment.sentiment_disagreement_threshold = 10.0

    news = FakeNews({"AAA": _arts("AAA", 4), "BBB": _arts("BBB", 3)})
    df = _base_frame()

    merged, s4, run_summary = _stage4_sentiment(df, news, _comparison_runtime(), config)

    # Stage-4 produced both models' columns and a run-level summary.
    assert "vader_sentiment_score" in merged.columns
    assert "finbert_sentiment_score" in merged.columns
    assert run_summary["comparison_mode"] is True
    assert run_summary["finbert_available"] is True
    assert run_summary["articles_scored_finbert"] == 7
    assert run_summary["sentiment_model_disagreement_count"] >= 1

    ranked, s5 = _stage5_compose_and_rank(merged, config)
    # Comparison flags merged into the per-row flags list.
    all_flags = [f for fl in ranked["flags"] for f in fl]
    assert "SENTIMENT_MODEL_DISAGREEMENT" in all_flags

    summary = _build_summary(
        ranked, config, mode="test", sample_limit=None, stage_stats=[s4, s5],
        exchange_breakdown={}, total_runtime=0.0, sentiment_run_summary=run_summary,
    )
    assert summary["sentiment_summary"]["comparison_mode"] is True
    cand = summary["candidates"][0]
    for key in (
        "vader_sentiment_score", "finbert_sentiment_score", "sentiment_score_delta",
        "sentiment_model_agreement", "final_sentiment_score", "sentiment_model_used",
    ):
        assert key in cand

    # Validation surfaces the sentiment-model comparison check.
    report = validate_outputs(ranked, summary, config)
    names = {c["name"] for c in report["checks"]}
    assert "sentiment_model_comparison" in names

    # Markdown note names the model and reports FinBERT availability.
    note = render_sentiment_model_note(run_summary)
    assert "Sentiment model" in note
    assert "FinBERT available" in note
