"""Sentiment analysis for news headlines / summaries.

Default model is VADER (lexicon-based, free). FinBERT can be plugged in via
:class:`SentimentModel` — see ``sentiment_model.py``.
"""

from .comparison import (
    SentimentRuntime,
    build_run_sentiment_summary,
    build_sentiment_runtime,
    is_finbert_available,
    resolve_ticker_sentiment,
)
from .sentiment_model import (
    ArticleSentiment,
    FinBertSentimentModel,
    SentimentModel,
    TickerSentiment,
    VaderSentimentModel,
    aggregate_ticker_sentiment,
    get_sentiment_model,
    score_articles,
)
from .text_preprocessing import clean_text

__all__ = [
    "ArticleSentiment",
    "FinBertSentimentModel",
    "SentimentModel",
    "SentimentRuntime",
    "TickerSentiment",
    "VaderSentimentModel",
    "aggregate_ticker_sentiment",
    "build_run_sentiment_summary",
    "build_sentiment_runtime",
    "clean_text",
    "get_sentiment_model",
    "is_finbert_available",
    "resolve_ticker_sentiment",
    "score_articles",
]
