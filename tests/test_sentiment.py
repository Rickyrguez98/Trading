"""Sentiment scoring + aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from asset_selection.data_providers.base import NewsItem
from asset_selection.sentiment.sentiment_model import (
    SentimentModel,
    aggregate_ticker_sentiment,
    score_articles,
)


class DummyModel(SentimentModel):
    """Deterministic stub so the test is reproducible without VADER's lexicon."""

    def __init__(self, scores):
        self._scores = list(scores)
        self._idx = 0

    def score(self, text: str) -> float:  # noqa: ARG002
        v = self._scores[self._idx % len(self._scores)]
        self._idx += 1
        return v


def _articles(n: int, source_prefix: str = "WireA"):
    base = datetime.now(timezone.utc)
    return [
        NewsItem(
            ticker="ZZZ",
            headline=f"Headline {i}",
            summary=f"Body {i}",
            source=f"{source_prefix}{i % 3}",
            url=f"https://example.com/{i}",
            published_at=(base - timedelta(days=i)).isoformat(),
            retrieved_at=base.isoformat(),
        )
        for i in range(n)
    ]


def test_score_articles_uses_provided_model():
    arts = _articles(3)
    model = DummyModel([0.8, -0.5, 0.1])
    scored = score_articles(arts, model)
    assert [s.label for s in scored] == ["positive", "negative", "neutral"]


def test_aggregate_empty_returns_neutral():
    agg = aggregate_ticker_sentiment("ZZZ", [], recency_halflife_days=7.0)
    assert agg.article_count == 0
    assert agg.sentiment_score == 50.0


def test_aggregate_recency_weighting_favors_recent_news():
    arts = _articles(2)
    # Most-recent article (day 0) is negative, older (day 1) is very positive.
    # With short halflife, recency-weighted compound must be negative-leaning.
    model = DummyModel([-0.6, +0.6])
    scored = score_articles(arts, model)
    agg = aggregate_ticker_sentiment("ZZZ", scored, recency_halflife_days=1.0)
    assert agg.recency_weighted_compound < agg.average_compound
    assert agg.sentiment_score < 50.0


def test_aggregate_confidence_scales_with_count_and_diversity():
    # Single article from single source -> low confidence.
    low = aggregate_ticker_sentiment(
        "ZZZ", score_articles(_articles(1), DummyModel([0.4])),
        recency_halflife_days=7.0, min_articles_for_confidence=3,
    )
    # Many articles from diverse sources -> higher confidence.
    high = aggregate_ticker_sentiment(
        "ZZZ", score_articles(_articles(5, "WireB"), DummyModel([0.2])),
        recency_halflife_days=7.0, min_articles_for_confidence=3,
    )
    assert high.confidence > low.confidence
    assert 0.0 <= low.confidence <= 1.0
    assert 0.0 <= high.confidence <= 1.0
