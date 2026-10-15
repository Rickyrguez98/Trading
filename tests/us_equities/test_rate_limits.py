"""Rate limiting end to end: providers pause and continue, a blocked feed is skipped fast.

Reproduces the 2026-10-15 run: the keyless news feed answered 429 to every
ticker, each failure cost ~7s of retries (16 minutes for 150 tickers) and
150 warning lines. Now the feed is paused after two attempts and the rest of
stage 4 takes no time, with one warning.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from trading_common.throttle import ProviderPaused, get_throttle

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Chipmaker beats estimates</title><link>https://example.com/a</link>
<pubDate>%s</pubDate></item></channel></rss>"""


class _Resp:
    def __init__(self, status=200, content=b"", payload=None, headers=None):
        self.status_code, self.content, self._payload = status, content, payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


def _rss_ok():
    stamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return _Resp(200, RSS % stamp.encode())


def _scripted(monkeypatch, *responses):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# News feed
# ---------------------------------------------------------------------------

def test_a_blocked_news_feed_is_paused_not_retried_per_ticker(monkeypatch, _throttle_state):
    from asset_selection.data_providers.news_provider import YahooRssNewsProvider

    calls = _scripted(monkeypatch, _Resp(429))
    p = YahooRssNewsProvider()
    with pytest.raises(requests.HTTPError, match="429"):
        p.fetch("AAPL")
    sent = len(calls)
    assert sent == 2 and len(_throttle_state) == 1          # one attempt, one pause, one retry
    for t in ("MSFT", "NVDA", "GOOGL"):
        with pytest.raises(ProviderPaused):
            p.fetch(t)
    assert len(calls) == sent                               # no request while paused


def test_retry_after_from_the_feed_is_honoured(monkeypatch, _throttle_state):
    from asset_selection.data_providers.news_provider import YahooRssNewsProvider

    _scripted(monkeypatch, _Resp(429, headers={"Retry-After": "7"}), _rss_ok())
    items = YahooRssNewsProvider().fetch("AAPL")
    assert [i.headline for i in items] == ["Chipmaker beats estimates"]
    assert 7 <= _throttle_state[0] <= 8


def test_finnhub_waits_out_its_per_minute_limit(monkeypatch, _throttle_state):
    from asset_selection.data_providers.news_provider import FinnhubNewsProvider

    monkeypatch.setenv("FINNHUB_API_KEY", "synthetic")
    now = int(datetime.now(timezone.utc).timestamp())
    article = {"headline": "Guidance raised", "datetime": now, "source": "Reuters", "url": "u"}
    _scripted(monkeypatch, _Resp(429), _Resp(200, payload=[article]))
    items = FinnhubNewsProvider().fetch("NVDA")
    assert items[0].headline == "Guidance raised"
    assert 15 <= _throttle_state[0] <= 17                   # finnhub's first pause


def test_a_429_message_never_contains_the_url(monkeypatch):
    from asset_selection.data_providers.news_provider import _raise_for_status

    with pytest.raises(requests.HTTPError) as exc:
        _raise_for_status(_Resp(429, headers={"Retry-After": "30"}))
    assert "http" not in str(exc.value).lower() and exc.value.retry_after == 30.0


# ---------------------------------------------------------------------------
# Prices and fundamentals: pause, then the run continues
# ---------------------------------------------------------------------------

def _yf_rate_limit():
    from yfinance.exceptions import YFRateLimitError
    return YFRateLimitError()


def test_prices_pause_on_yahoo_rate_limit_and_still_get_the_price(monkeypatch, _throttle_state):
    from asset_selection.data_providers.prices_provider import YFinancePricesProvider

    idx = pd.bdate_range(end=pd.Timestamp("2026-10-14"), periods=60)
    frame = pd.DataFrame({"Close": range(100, 160), "Volume": [1_000_000] * 60}, index=idx)
    outcomes = [_yf_rate_limit(), frame]

    def fake_history(self, provider_symbol, lookback_days):
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(YFinancePricesProvider, "_download_history", fake_history)
    snap = YFinancePricesProvider().fetch("AAPL", lookback_days=60)
    assert snap.status == "ok" and snap.last_close == 159
    assert 30 <= _throttle_state[0] <= 32                    # paused 30s instead of failing


def test_fundamentals_pause_and_continue(monkeypatch, _throttle_state):
    from asset_selection.data_providers.fundamentals_provider import YFinanceFundamentalsProvider

    outcomes = [_yf_rate_limit(), {"longName": "Apple Inc.", "sector": "Technology", "marketCap": 3e12}]

    def fake_info(self, provider_symbol):
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(YFinanceFundamentalsProvider, "_fetch_info", fake_info)
    f = YFinanceFundamentalsProvider().fetch("AAPL")
    assert f.status == "ok" and f.sector == "Technology"
    assert len(_throttle_state) == 1


def test_prices_and_fundamentals_share_one_yahoo_pause():
    """Yahoo limits them together, so a pause seen by one applies to the other."""
    import asset_selection.data_providers.fundamentals_provider as fp
    import asset_selection.data_providers.prices_provider as pp
    import inspect

    assert 'get_throttle("yfinance")' in inspect.getsource(pp.YFinancePricesProvider)
    assert 'get_throttle("yfinance")' in inspect.getsource(fp.YFinanceFundamentalsProvider)


def test_quick_retries_do_not_hammer_a_rate_limited_endpoint(monkeypatch, _throttle_state):
    """tenacity used to retry a 429 three times in ~3s; now only the throttle reacts."""
    from asset_selection.data_providers.news_provider import YahooRssNewsProvider

    calls = _scripted(monkeypatch, _Resp(429))
    with pytest.raises(requests.HTTPError):
        YahooRssNewsProvider().fetch("AAPL")
    assert len(calls) == 2                                   # was 6 (3 tenacity tries x 2)


# ---------------------------------------------------------------------------
# Stage 4: one warning, not one per ticker
# ---------------------------------------------------------------------------

def test_stage4_logs_a_paused_feed_once(caplog):
    from asset_selection.config import load_config
    from asset_selection.pipelines.run_asset_selection import _stage4_sentiment
    from asset_selection.sentiment.comparison import build_sentiment_runtime

    get_throttle("yahoo_rss")                                # registry entry exists

    class PausedNews:
        def fetch(self, ticker, max_age_days=30):
            raise ProviderPaused("yahoo_rss paused after repeated 429 Too Many Requests (rate limited)")

    config = load_config(None)
    df = pd.DataFrame({"ticker": [f"T{i}" for i in range(40)]})
    with caplog.at_level(logging.DEBUG):
        merged, stats, _ = _stage4_sentiment(df, PausedNews(), build_sentiment_runtime(config.sentiment), config)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "news fetch raised" in r.message]
    assert warnings == []
    assert len(merged) == 40                                  # every ticker still scored (neutral)
    assert sum(stats.failure_reasons.values()) == 40       # still recorded as failures


def test_pacing_defaults():
    from asset_selection.config import load_config

    for source in (None, "configs/us_equities/default.yaml"):
        rl = load_config(source).rate_limits
        assert rl["yfinance"] == 0.4 and rl["yahoo_rss"] == 2.0 and rl["finnhub"] == 1.1
    # A user YAML that sets only one value keeps the others.
    assert load_config({"rate_limits": {"yfinance": 1.0}}).rate_limits["finnhub"] == 1.1


def test_after_the_cooldown_the_feed_is_tried_again(monkeypatch):
    from asset_selection.data_providers.news_provider import YahooRssNewsProvider

    _scripted(monkeypatch, _Resp(429), _Resp(429), _rss_ok())
    p = YahooRssNewsProvider()
    with pytest.raises(requests.HTTPError):
        p.fetch("AAPL")
    t = get_throttle("yahoo_rss")
    assert t.paused_until is not None
    t.paused_until = t._clock() - 1                           # cooldown elapsed
    assert p.fetch("MSFT")[0].headline == "Chipmaker beats estimates"
    assert t.paused_until is None
    _ = timedelta  # keep the import list honest for readers comparing timings
