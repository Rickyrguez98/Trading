"""News providers: Finnhub, the keyless Yahoo RSS feed, and the honesty contract.

Offline by construction — the HTTP call is stubbed, so these assert parsing
and the provider-failure contract, never live coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from asset_selection.data_providers import get_news_provider
from asset_selection.data_providers.news_provider import (
    FinnhubNewsProvider,
    NewsProviderNotConfigured,
    YahooRssNewsProvider,
    _rfc822_to_iso,
)
from asset_selection.data_providers.symbols import to_provider_symbol

RSS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>AAPL - Latest News</title>
  <item>
    <title>Apple beats expectations</title>
    <description>Record quarter on services growth.</description>
    <link>https://example.com/a</link>
    <pubDate>{recent}</pubDate>
    <source>Reuters</source>
  </item>
  <item>
    <title>Old stale story</title>
    <description>Ancient history.</description>
    <link>https://example.com/b</link>
    <pubDate>{old}</pubDate>
  </item>
  <item>
    <title></title>
    <description>No headline, must be skipped.</description>
    <pubDate>{recent}</pubDate>
  </item>
</channel></rss>
"""


def _rfc822(dt: datetime) -> str:
    from email.utils import format_datetime
    return format_datetime(dt)


def _feed_xml() -> bytes:
    now = datetime.now(timezone.utc)
    return RSS_TEMPLATE.format(
        recent=_rfc822(now - timedelta(hours=3)),
        old=_rfc822(now - timedelta(days=400)),
    ).encode()


class _Resp:
    def __init__(self, content=b"", status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _provider(monkeypatch, resp):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"], captured["headers"] = url, headers
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    return YahooRssNewsProvider(), captured


def test_registered_and_uses_yahoo_symbol_convention():
    assert get_news_provider("yahoo_rss") is YahooRssNewsProvider
    assert to_provider_symbol("BRK.B", "yahoo_rss") == "BRK-B"


def test_parses_headlines_and_applies_the_age_cutoff(monkeypatch):
    p, captured = _provider(monkeypatch, _Resp(_feed_xml()))
    items = p.fetch("AAPL", max_age_days=30)

    # the stale (400-day) item and the empty-title item are both dropped
    assert len(items) == 1
    it = items[0]
    assert it.ticker == "AAPL"
    assert it.headline == "Apple beats expectations"
    assert it.summary == "Record quarter on services growth."
    assert it.url == "https://example.com/a"
    assert it.source == "Reuters"          # from <source>, not invented
    assert it.published_at and it.retrieved_at
    assert "s=AAPL" in captured["url"]
    assert "Mozilla" in captured["headers"]["User-Agent"]


def test_missing_source_stays_none_rather_than_being_invented(monkeypatch):
    xml = """<?xml version="1.0"?><rss><channel>
      <item><title>No publisher given</title><pubDate>{d}</pubDate></item>
    </channel></rss>""".format(d=_rfc822(datetime.now(timezone.utc)))
    p, _ = _provider(monkeypatch, _Resp(xml.encode()))
    items = p.fetch("AAPL")
    assert len(items) == 1 and items[0].source is None


def test_empty_feed_is_empty_news_not_an_error(monkeypatch):
    xml = b'<?xml version="1.0"?><rss><channel><title>x</title></channel></rss>'
    p, _ = _provider(monkeypatch, _Resp(xml))
    assert p.fetch("AAPL") == []


@pytest.mark.parametrize("status", [404, 429, 500])
def test_http_failures_are_raised_not_swallowed(monkeypatch, status):
    """Contract: a provider outage must be distinguishable from 'no news'."""
    p, _ = _provider(monkeypatch, _Resp(b"", status))
    with pytest.raises(requests.HTTPError):
        p.fetch("AAPL")


def test_malformed_xml_raises(monkeypatch):
    p, _ = _provider(monkeypatch, _Resp(b"<<not xml>>"))
    with pytest.raises(Exception):
        p.fetch("AAPL")


def test_results_are_cached(monkeypatch, tmp_path):
    from trading_common.cache import Cache

    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(_feed_xml())

    monkeypatch.setattr(requests, "get", fake_get)
    p = YahooRssNewsProvider(cache=Cache(directory=str(tmp_path), enabled=True))
    a = p.fetch("AAPL", max_age_days=30)
    b = p.fetch("AAPL", max_age_days=30)
    assert calls["n"] == 1
    assert [i.headline for i in a] == [i.headline for i in b]


@pytest.mark.parametrize("raw,expected_year", [
    ("Tue, 07 Oct 2025 14:30:00 GMT", 2025),
    ("Tue, 07 Oct 2025 14:30:00 -0400", 2025),
])
def test_rfc822_parsing(raw, expected_year):
    iso = _rfc822_to_iso(raw)
    assert iso and datetime.fromisoformat(iso).year == expected_year
    assert datetime.fromisoformat(iso).tzinfo is not None


@pytest.mark.parametrize("bad", [None, "", "not a date", "   "])
def test_rfc822_rejects_garbage(bad):
    assert _rfc822_to_iso(bad) is None


# ---------------------------------------------------------------------------
# Provider selection / chaining
# ---------------------------------------------------------------------------

def test_finnhub_is_the_default_news_provider():
    """Finnhub is the only backend whose terms permit automated access.

    yfinance's private news endpoint was retired by Yahoo (HTTP 404) and the
    keyless RSS feed is best-effort, so the documented API is the default.
    """
    from asset_selection.config import load_config

    assert load_config({}).providers.news == "finnhub"
    assert load_config("configs/us_equities/default.yaml").providers.news == "finnhub"


def _build_news(cfg):
    from trading_common.cache import Cache
    from trading_common.rate_limiter import RateLimiter

    from asset_selection.data_providers import build_news_provider

    return build_news_provider(
        cfg, lambda ns: Cache(enabled=False), lambda n: RateLimiter(0)
    )


def test_shipped_config_chains_a_keyless_fallback_behind_finnhub():
    """Without a key, sentiment must still have a route to real articles."""
    from asset_selection.config import load_config
    from asset_selection.data_providers.fallback import FallbackNewsProvider

    provider = _build_news(load_config("configs/us_equities/default.yaml"))
    assert isinstance(provider, FallbackNewsProvider)
    assert [type(p).__name__ for p in provider.providers] == [
        "FinnhubNewsProvider", "YahooRssNewsProvider",
    ]


def test_news_providers_can_be_chained_as_a_fallback():
    from asset_selection.config import load_config
    from asset_selection.data_providers.fallback import FallbackNewsProvider

    cfg = load_config({
        "providers": {"news": "yahoo_rss"},
        "robustness": {"provider_priority_by_data_type": {"news": ["yahoo_rss", "yfinance"]}},
    })
    provider = _build_news(cfg)
    assert isinstance(provider, FallbackNewsProvider)
    assert [type(p).__name__ for p in provider.providers] == [
        "YahooRssNewsProvider", "YFinanceNewsProvider",
    ]


# ---------------------------------------------------------------------------
# Finnhub (documented API, requires FINNHUB_API_KEY)
# ---------------------------------------------------------------------------

def _finnhub_article(dt: datetime, headline="Chipmaker lifts guidance"):
    return {
        "category": "company",
        "datetime": int(dt.timestamp()),
        "headline": headline,
        "id": 1,
        "source": "Reuters",
        "summary": "Demand for accelerators stayed strong.",
        "url": "https://example.com/finnhub-a",
    }


class _JsonResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _finnhub(monkeypatch, resp, key="test-key-123"):
    captured = {}
    if key is None:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FINNHUB_API_KEY", key)

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return resp

    monkeypatch.setattr(requests, "get", fake_get)
    return FinnhubNewsProvider(), captured


def test_finnhub_is_registered_and_keeps_dotted_class_shares():
    assert get_news_provider("finnhub") is FinnhubNewsProvider
    # Finnhub spells class shares with a dot, unlike Yahoo's hyphen form.
    assert to_provider_symbol("BRK.B", "finnhub") == "BRK.B"


def test_finnhub_parses_articles_with_a_real_source(monkeypatch):
    now = datetime.now(timezone.utc)
    resp = _JsonResp([
        _finnhub_article(now - timedelta(days=1)),
        _finnhub_article(now - timedelta(days=400), headline="Ancient news"),
        _finnhub_article(now, headline=""),          # no headline -> skipped
    ])
    p, captured = _finnhub(monkeypatch, resp)
    items = p.fetch("NVDA", max_age_days=30)

    assert [i.headline for i in items] == ["Chipmaker lifts guidance"]
    only = items[0]
    assert only.ticker == "NVDA"
    # Unlike the RSS feed, Finnhub gives a genuine per-article publisher, so the
    # source-diversity term in the confidence model is meaningful.
    assert only.source == "Reuters"
    assert only.summary and only.url and only.published_at
    assert captured["params"]["symbol"] == "NVDA"


def test_finnhub_sends_the_key_in_a_header_never_in_the_url(monkeypatch):
    """The key must not leak into URLs, logs, or cache keys."""
    p, captured = _finnhub(monkeypatch, _JsonResp([]), key="super-secret")
    p.fetch("AAPL")

    assert captured["headers"]["X-Finnhub-Token"] == "super-secret"
    assert "super-secret" not in captured["url"]
    assert "super-secret" not in str(captured["params"])
    assert "token" not in {k.lower() for k in (captured["params"] or {})}


def test_finnhub_without_a_key_raises_before_any_network_call(monkeypatch):
    """A missing key is a config fault, never a silent 'this name has no news'."""
    called = []
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: called.append(1))

    with pytest.raises(NewsProviderNotConfigured, match="FINNHUB_API_KEY"):
        FinnhubNewsProvider().fetch("AAPL")
    assert called == [], "no HTTP request may be made without a key"


def test_finnhub_blank_key_is_treated_as_missing(monkeypatch):
    p, _ = _finnhub(monkeypatch, _JsonResp([]), key="   ")
    with pytest.raises(NewsProviderNotConfigured):
        p.fetch("AAPL")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_finnhub_http_failures_are_raised_not_swallowed(monkeypatch, status):
    p, _ = _finnhub(monkeypatch, _JsonResp([], status))
    with pytest.raises(requests.HTTPError):
        p.fetch("AAPL")


def test_finnhub_error_payload_is_raised(monkeypatch):
    """A 200 carrying {"error": ...} must not read as 'no news'."""
    p, _ = _finnhub(monkeypatch, _JsonResp({"error": "Invalid API key"}))
    with pytest.raises(RuntimeError, match="Invalid API key"):
        p.fetch("AAPL")


def test_finnhub_empty_list_is_honest_no_news(monkeypatch):
    p, _ = _finnhub(monkeypatch, _JsonResp([]))
    assert p.fetch("AAPL") == []


def test_keyless_chain_falls_through_finnhub_to_the_rss_feed(monkeypatch):
    """The shipped default must still reach real articles with no key set."""
    from asset_selection.config import load_config

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    now = datetime.now(timezone.utc)
    xml = RSS_TEMPLATE.format(
        recent=_rfc822(now - timedelta(hours=3)),
        old=_rfc822(now - timedelta(days=400)),
    ).encode()
    monkeypatch.setattr(
        requests, "get",
        lambda url, params=None, headers=None, timeout=None: _Resp(xml),
    )

    provider = _build_news(load_config("configs/us_equities/default.yaml"))
    items = provider.fetch("AAPL", max_age_days=30)
    assert [i.headline for i in items] == ["Apple beats expectations"]
