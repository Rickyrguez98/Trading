"""NewsAPI.ai (Event Registry) news provider, against a stubbed HTTP layer.

Live responses are not exercised here (that needs a personal key and spends
tokens from a 2,000-token plan); the request it sends and its handling of
answers, errors, missing keys and the per-run budget are.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from asset_selection.data_providers import get_news_provider
from asset_selection.data_providers.fallback import FallbackNewsProvider
from asset_selection.data_providers.news_provider import (
    NewsApiAiProvider,
    NewsProviderNotConfigured,
    search_name,
)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.headers = payload, status, {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


def _articles(*titles, days_old=1):
    when = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"articles": {"results": [
        {"title": t, "body": "Body text", "url": f"https://example.com/{i}", "dateTimePub": when,
         "source": {"uri": "reuters.com", "title": "Reuters"}} for i, t in enumerate(titles)],
        "totalResults": len(titles)}}


class _Calls(list):
    """Requests sent, plus queued answers for the next ones."""

    def __init__(self):
        super().__init__()
        self.answers = []


@pytest.fixture
def posted(monkeypatch):
    calls = _Calls()
    answers = calls.answers

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return answers.pop(0) if answers else _Resp(_articles("Nvidia beats estimates"))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(NewsApiAiProvider._download.retry, "sleep", lambda seconds: None)   # no real backoff
    monkeypatch.setenv("NEWSAPI_AI_KEY", "synthetic-key")
    return calls


def test_registered_and_searches_headlines_by_company_name(posted):
    assert get_news_provider("newsapi_ai") is NewsApiAiProvider
    p = NewsApiAiProvider()
    p.use_company_names({"NVDA": "NVIDIA Corporation - Common Stock"})
    items = p.fetch("nvda", max_age_days=30)
    assert [i.headline for i in items] == ["Nvidia beats estimates"]
    assert items[0].source == "Reuters" and items[0].ticker == "NVDA" and items[0].published_at
    body = posted[0]["json"]
    assert posted[0]["url"] == "https://eventregistry.org/api/v1/article/getArticles"
    assert "synthetic-key" not in posted[0]["url"]                         # key in the body, not the URL
    assert body["apiKey"] == "synthetic-key" and body["keyword"] == "NVIDIA"
    assert body["keywordLoc"] == "title" and body["lang"] == "eng" and body["resultType"] == "articles"
    assert body["isDuplicateFilter"] == "skipDuplicates" and body["forceMaxDataTimeWindow"] == 31


def test_unknown_name_falls_back_to_the_ticker_and_old_articles_are_dropped(posted):
    posted.answers.append(_Resp(_articles("Old story", days_old=60)))
    assert NewsApiAiProvider().fetch("VST", max_age_days=30) == []
    assert posted[0]["json"]["keyword"] == "VST"


def test_missing_key_fails_before_any_request(posted, monkeypatch):
    monkeypatch.delenv("NEWSAPI_AI_KEY")
    with pytest.raises(NewsProviderNotConfigured):
        NewsApiAiProvider().fetch("NVDA")
    assert posted == []


@pytest.mark.parametrize("answer", [_Resp({"error": "Invalid API key"}), _Resp({}, status=401),
                                    _Resp({"articles": {}})])
def test_errors_are_failures_never_empty_news(posted, answer):
    posted.answers.extend([answer] * 3)
    with pytest.raises((RuntimeError, requests.HTTPError)):
        NewsApiAiProvider().fetch("NVDA")


def test_per_run_budget_protects_the_plan(posted, monkeypatch):
    monkeypatch.setenv("NEWSAPI_AI_MAX_CALLS", "2")
    p = NewsApiAiProvider()
    p.fetch("AAA")
    p.fetch("BBB")
    with pytest.raises(NewsProviderNotConfigured, match="budget"):
        p.fetch("CCC")
    assert len(posted) == 2


def test_answers_are_cached_for_a_day(posted, tmp_path):
    from trading_common.cache import Cache

    cache = Cache(directory=str(tmp_path), enabled=True, default_ttl=3600)
    NewsApiAiProvider(cache=cache).fetch("NVDA")
    NewsApiAiProvider(cache=cache).fetch("NVDA")
    assert len(posted) == 1 and NewsApiAiProvider.cache_ttl_seconds == 86400


def test_a_chain_passes_company_names_on(posted):
    first = NewsApiAiProvider()
    chain = FallbackNewsProvider([first])
    chain.use_company_names({"PLTR": "Palantir Technologies Inc. Class A Common Stock"})
    chain.fetch("PLTR")
    assert posted[0]["json"]["keyword"] == "Palantir"


@pytest.mark.parametrize("raw, expected", [
    ("Palantir Technologies Inc. - Class A Common Stock", "Palantir"),
    ("Meta Platforms, Inc.", "Meta"),
    ("Capital One Financial Corporation", "Capital One"),
    ("Frontline Plc Ordinary Shares", "Frontline"),
    ("AST SpaceMobile, Inc.", "AST SpaceMobile"),
    ("Vistra Corp", "Vistra"),
    (None, None),
])
def test_search_name(raw, expected):
    assert search_name(raw) == expected


def test_shipped_chain_tries_newsapi_ai_first():
    from asset_selection.config import load_config

    cfg = load_config("configs/us_equities/default.yaml")
    assert cfg.robustness.provider_priority_by_data_type["news"] == ["newsapi_ai", "finnhub", "yahoo_rss"]
    assert cfg.rate_limits["newsapi_ai"] == 0.5
