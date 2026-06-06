"""Sentiment scoring with a pluggable backend.

- :class:`VaderSentimentModel` — default; lexicon-based; no extra deps.
- :class:`FinBertSentimentModel` — optional; requires ``transformers`` + ``torch``
  (install with ``pip install '.[finbert]'``).

We score article-by-article and then aggregate per ticker with recency weighting.
The aggregate output is on a 0..100 scale to match the rest of the scoring system.
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from ..data_providers.base import NewsItem
from .text_preprocessing import clean_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArticleSentiment:
    """Sentiment of a single article, normalized to [-1, +1]."""
    ticker: str
    headline: str
    compound: float                  # -1..+1
    label: str                       # 'positive' | 'neutral' | 'negative'
    source: Optional[str] = None
    published_at: Optional[str] = None
    url: Optional[str] = None
    retrieved_at: Optional[str] = None
    is_duplicate: bool = False       # same headline/url already seen for this ticker
    is_stale: bool = False           # older than the configured staleness window
    age_days: Optional[float] = None


@dataclass
class TickerSentiment:
    """Aggregated sentiment for a ticker, on a 0..100 scale."""
    ticker: str
    sentiment_score: float = 50.0    # 0..100; 50 = neutral
    average_compound: float = 0.0
    recency_weighted_compound: float = 0.0
    article_count: int = 0
    unique_article_count: int = 0
    duplicate_count: int = 0
    stale_count: int = 0
    fresh_ratio: float = 0.0         # share of non-stale articles
    unique_ratio: float = 0.0        # share of non-duplicate articles
    positive_ratio: float = 0.0
    negative_ratio: float = 0.0
    neutral_ratio: float = 0.0
    source_diversity: int = 0
    confidence: float = 0.0          # 0..1
    model_name: str = "vader"
    articles: List[ArticleSentiment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class SentimentModel(ABC):
    """Strategy interface so backends are swappable in one config line."""

    @abstractmethod
    def score(self, text: str) -> float:
        """Return a compound polarity score in [-1, +1] for the input string."""


class VaderSentimentModel(SentimentModel):
    """VADER lexicon — general-purpose English sentiment. Light and free."""

    def __init__(self) -> None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._analyzer = SentimentIntensityAnalyzer()

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        return float(self._analyzer.polarity_scores(text)["compound"])


class FinBertSentimentModel(SentimentModel):
    """FinBERT — finance-tuned. Heavy (~440MB) and slow on CPU.

    Install: ``pip install '.[finbert]'``. Lazy-loaded so the default
    pipeline doesn't pay the import cost.
    """

    def __init__(self, model_name: str = "yiyanghkust/finbert-tone") -> None:
        try:
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )
        except ImportError as exc:
            raise ImportError(
                "FinBERT requires the [finbert] extras. "
                "Install with: pip install '.[finbert]'"
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self._pipe = pipeline("sentiment-analysis", model=model, tokenizer=tokenizer)

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        result = self._pipe(text[:512])[0]  # FinBERT context limit
        label = str(result.get("label", "")).lower()
        confidence = float(result.get("score", 0.0))
        if "positive" in label:
            return +confidence
        if "negative" in label:
            return -confidence
        return 0.0


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_sentiment_model(name: str = "vader") -> SentimentModel:
    if name == "vader":
        return VaderSentimentModel()
    if name == "finbert":
        return FinBertSentimentModel()
    raise ValueError(f"Unknown sentiment model: {name!r}")


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_POS_THRESHOLD = 0.05
_NEG_THRESHOLD = -0.05


def _label(compound: float) -> str:
    if compound >= _POS_THRESHOLD:
        return "positive"
    if compound <= _NEG_THRESHOLD:
        return "negative"
    return "neutral"


def score_articles(
    articles: Iterable[NewsItem],
    model: SentimentModel,
) -> List[ArticleSentiment]:
    """Score each article and return a list of ArticleSentiment."""
    scored: List[ArticleSentiment] = []
    for art in articles:
        text = clean_text(" ".join(filter(None, [art.headline, art.summary])))
        if not text:
            continue
        compound = model.score(text)
        scored.append(
            ArticleSentiment(
                ticker=art.ticker,
                headline=art.headline,
                compound=compound,
                label=_label(compound),
                source=art.source,
                published_at=art.published_at,
                url=art.url,
                retrieved_at=art.retrieved_at,
            )
        )
    return scored


def _dedup_key(art: ArticleSentiment) -> str:
    """Identity used for duplicate detection.

    Wire stories are frequently re-published verbatim across aggregators, so we
    key on the normalized URL when present, otherwise the normalized headline.
    """
    if art.url:
        return "url:" + art.url.strip().lower().rstrip("/")
    return "head:" + " ".join((art.headline or "").lower().split())


def aggregate_ticker_sentiment(
    ticker: str,
    scored: Sequence[ArticleSentiment],
    *,
    recency_halflife_days: float = 7.0,
    min_articles_for_confidence: int = 3,
    confidence_full_article_count: int = 25,
    confidence_full_source_count: int = 5,
    stale_after_days: float = 14.0,
    model_confidence_factor: float = 0.85,
    model_name: str = "vader",
    now: Optional[datetime] = None,
) -> TickerSentiment:
    """Roll up article-level scores into a single TickerSentiment record.

    sentiment_score = 50 + 50 * recency_weighted_compound, clipped to [0,100].

    Confidence is in [0,1] and is deliberately hard to max out. It blends five
    signals so a ticker can't look "certain" off a thin, stale, duplicated feed:

      * article volume   -- UNIQUE articles vs ``confidence_full_article_count``
      * source diversity -- distinct sources vs ``confidence_full_source_count``
      * freshness        -- share of articles inside ``stale_after_days``
      * de-duplication   -- share of articles that aren't repeats
      * model quality    -- ``model_confidence_factor`` ceiling (lexicon < FinBERT)

    The old model saturated at 3 articles from any source, so 10 near-identical
    yfinance headlines scored 1.0. That is the bug this addresses.
    """
    if not scored:
        return TickerSentiment(ticker=ticker, model_name=model_name, articles=[])

    now = now or datetime.now(timezone.utc)

    # Mark duplicates (first occurrence kept, later repeats flagged) and stale
    # articles, and stamp each article's age in days for downstream reporting.
    seen: set = set()
    weights: List[float] = []
    for art in scored:
        key = _dedup_key(art)
        art.is_duplicate = key in seen
        seen.add(key)

        published_at = _parse_iso(art.published_at) if art.published_at else None
        if published_at is None:
            art.age_days = None
            art.is_stale = False
            weights.append(0.5)  # neutral weight for undated articles
            continue
        age_days = max((now - published_at).total_seconds() / 86400.0, 0.0)
        art.age_days = age_days
        art.is_stale = age_days > stale_after_days
        # half-life decay: w = 0.5 ** (age / halflife)
        decay = math.pow(0.5, age_days / max(recency_halflife_days, 0.01))
        weights.append(decay)

    total_w = sum(weights) or 1e-9
    avg_compound = sum(a.compound for a in scored) / len(scored)
    recency_weighted = sum(a.compound * w for a, w in zip(scored, weights)) / total_w

    pos = sum(1 for a in scored if a.label == "positive")
    neg = sum(1 for a in scored if a.label == "negative")
    neu = sum(1 for a in scored if a.label == "neutral")
    n = len(scored)

    unique = [a for a in scored if not a.is_duplicate]
    unique_n = len(unique)
    duplicate_count = n - unique_n
    stale_count = sum(1 for a in scored if a.is_stale)
    fresh_ratio = (n - stale_count) / n
    unique_ratio = unique_n / n
    # Diversity counts only the sources of NON-duplicate articles.
    sources = {a.source for a in unique if a.source}

    # --- Confidence: a weighted blend, capped by model quality. -------------
    article_factor = min(unique_n / max(confidence_full_article_count, 1), 1.0)
    diversity_factor = min(len(sources) / max(confidence_full_source_count, 1), 1.0)
    raw_confidence = (
        0.40 * article_factor
        + 0.25 * diversity_factor
        + 0.20 * fresh_ratio
        + 0.15 * unique_ratio
    )
    # Below the minimum article floor, damp confidence proportionally so a
    # single article can never look like a trustworthy consensus.
    if unique_n < min_articles_for_confidence:
        raw_confidence *= unique_n / max(min_articles_for_confidence, 1)
    confidence = max(0.0, min(1.0, raw_confidence * model_confidence_factor))

    sentiment_0_100 = max(0.0, min(100.0, 50.0 + 50.0 * recency_weighted))

    return TickerSentiment(
        ticker=ticker,
        sentiment_score=sentiment_0_100,
        average_compound=avg_compound,
        recency_weighted_compound=recency_weighted,
        article_count=n,
        unique_article_count=unique_n,
        duplicate_count=duplicate_count,
        stale_count=stale_count,
        fresh_ratio=fresh_ratio,
        unique_ratio=unique_ratio,
        positive_ratio=pos / n,
        negative_ratio=neg / n,
        neutral_ratio=neu / n,
        source_diversity=len(sources),
        confidence=confidence,
        model_name=model_name,
        articles=list(scored),
    )


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        # Accept trailing 'Z'
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
