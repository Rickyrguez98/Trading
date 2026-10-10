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
    # 0.04 is below the |0.05| neutral threshold -> labeled neutral.
    model = DummyModel([0.8, -0.5, 0.04])
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


def _single_source_articles(n: int, source: str = "yahoo"):
    """n distinct, fresh articles all from ONE source (the yfinance pattern)."""
    base = datetime.now(timezone.utc)
    return [
        NewsItem(
            ticker="ZZZ",
            headline=f"Distinct headline {i}",
            summary=f"Body {i}",
            source=source,
            url=f"https://example.com/{i}",
            published_at=base.isoformat(),
            retrieved_at=base.isoformat(),
        )
        for i in range(n)
    ]


def test_ten_single_source_articles_do_not_reach_full_confidence():
    # The audited bug: 10 near-identical yfinance headlines reported 1.0.
    arts = _single_source_articles(10)
    agg = aggregate_ticker_sentiment(
        "ZZZ", score_articles(arts, DummyModel([0.3])),
        recency_halflife_days=7.0, min_articles_for_confidence=3,
        confidence_full_article_count=25, confidence_full_source_count=5,
        model_confidence_factor=0.85,
    )
    # Far below 1.0: only 10/25 of the volume and 1/5 of the diversity.
    assert agg.confidence < 0.6
    assert agg.source_diversity == 1
    assert agg.unique_article_count == 10
    assert agg.duplicate_count == 0


def test_duplicate_articles_are_flagged_and_lower_confidence():
    base = datetime.now(timezone.utc)
    dup = NewsItem(
        ticker="ZZZ", headline="Same story", summary="x", source="wire",
        url="https://example.com/same", published_at=base.isoformat(),
    )
    arts = [dup, dup, dup]  # three copies of the same wire story
    agg = aggregate_ticker_sentiment(
        "ZZZ", score_articles(arts, DummyModel([0.3])),
        recency_halflife_days=7.0, min_articles_for_confidence=3,
    )
    assert agg.article_count == 3
    assert agg.unique_article_count == 1
    assert agg.duplicate_count == 2
    assert agg.unique_ratio < 1.0
    # Two of three articles are marked duplicates.
    assert sum(1 for a in agg.articles if a.is_duplicate) == 2


def test_stale_articles_are_flagged_and_reduce_freshness():
    base = datetime.now(timezone.utc)
    arts = [
        NewsItem(ticker="ZZZ", headline="Old news", summary="x", source="a",
                 url="https://example.com/old",
                 published_at=(base - timedelta(days=40)).isoformat()),
        NewsItem(ticker="ZZZ", headline="Fresh news", summary="y", source="b",
                 url="https://example.com/new",
                 published_at=base.isoformat()),
    ]
    agg = aggregate_ticker_sentiment(
        "ZZZ", score_articles(arts, DummyModel([0.3])),
        recency_halflife_days=7.0, min_articles_for_confidence=1,
        stale_after_days=14.0,
    )
    assert agg.stale_count == 1
    assert agg.fresh_ratio == 0.5
    stale = [a for a in agg.articles if a.is_stale]
    assert len(stale) == 1 and stale[0].headline == "Old news"
