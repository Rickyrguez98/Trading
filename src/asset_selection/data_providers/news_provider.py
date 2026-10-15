"""News providers.

Three backends, in descending order of reliability:

* ``finnhub`` -- a **documented public API** whose terms permit programmatic
  access. Needs a free API key in ``FINNHUB_API_KEY``. This is the only
  backend here that is explicitly licensed for automated use, so it is the
  recommended one for any unattended run.
* ``yahoo_rss`` -- Yahoo's published RSS headline feed. Keyless, but see the
  access-policy note on :class:`YahooRssNewsProvider`: the feed host's
  ``robots.txt`` disallows automated agents, and it rate-limits hard
  (persistent HTTP 429) from datacenter IPs.
* ``yfinance`` -- Yahoo's *private* per-ticker news endpoint, which Yahoo has
  retired more than once. It currently answers HTTP 404, so this backend
  returns no news at all; it is kept only as a last link in a chain and for
  the historical record.

A news outage never invalidates a ranking -- it only lowers sentiment
confidence. Every provider here **raises** on a transport failure and returns
an empty list only when the call genuinely succeeded with no articles, so a
blocked provider can never masquerade as "this company has no news".

Historical note on the yfinance endpoint: it returns a list of dicts with keys
like ``title``, ``publisher``, ``link``, ``providerPublishTime`` (unix
seconds), ``summary`` (sometimes). Depth and freshness vary a lot per ticker --
we cap by ``max_age_days``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from trading_common.throttle import get_throttle, is_rate_limited, parse_retry_after

from .base import NewsItem, NewsProvider
from .symbols import to_provider_symbol

logger = logging.getLogger(__name__)


def _not_rate_limited(exc: BaseException) -> bool:
    """Quick retries are for transient faults; rate limits go to the throttle."""
    return not is_rate_limited(exc)


class NewsProviderNotConfigured(RuntimeError):
    """A provider needs a credential that is not present in the environment.

    Raised (never swallowed) so the condition is visible: a chain moves on to
    the next provider, and a single configured provider reports an honest
    failure instead of silently producing neutral sentiment.
    """


class YFinanceNewsProvider(NewsProvider):
    name = "yfinance"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8),
           retry=retry_if_exception(_not_rate_limited), reraise=True)
    def _download(self, provider_symbol: str) -> List[Dict[str, Any]]:
        import yfinance as yf

        self.rate_limiter.acquire()
        tk = yf.Ticker(provider_symbol)
        news = getattr(tk, "news", None) or []
        return list(news) if news else []

    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        ticker = ticker.strip().upper()
        provider_symbol = to_provider_symbol(ticker, self.name)
        cache_id = f"{provider_symbol}:{max_age_days}"
        cached = self._cache_get(cache_id)
        if cached is not None:
            return [NewsItem(**row) for row in cached]

        # NOTE: we deliberately do NOT swallow download errors here. A raised
        # exception means the *provider* failed (blocked/rate-limited/parse
        # error) and must be distinguishable from a genuinely empty news list
        # (the call succeeded, the name simply has no recent coverage). The
        # caller (stage 4) catches and classifies the exception via
        # errors.classify_exception so a systemic news outage is visible rather
        # than masquerading as "no news".
        raw = get_throttle("yfinance").call(self._download, provider_symbol)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        items: List[NewsItem] = []
        now_iso = self._now_iso()

        for entry in raw:
            # yfinance changed shape over versions: sometimes flat dicts, sometimes
            # nested under "content". Handle both.
            content = entry.get("content") if isinstance(entry, dict) else None
            data = content if isinstance(content, dict) else entry

            title = (
                data.get("title")
                or data.get("headline")
                or ""
            )
            if not title:
                continue

            published_dt = _parse_published(data)
            if published_dt is not None and published_dt < cutoff:
                continue

            summary = data.get("summary") or data.get("description") or None
            publisher = (
                data.get("publisher")
                or (data.get("provider") or {}).get("displayName")
                if isinstance(data.get("provider"), dict)
                else data.get("publisher")
            )
            url = (
                data.get("link")
                or data.get("canonicalUrl", {}).get("url")
                if isinstance(data.get("canonicalUrl"), dict)
                else data.get("link")
            )

            items.append(
                NewsItem(
                    ticker=ticker,
                    headline=str(title).strip(),
                    summary=str(summary).strip() if summary else None,
                    source=str(publisher).strip() if publisher else None,
                    url=str(url).strip() if url else None,
                    published_at=published_dt.isoformat() if published_dt else None,
                    retrieved_at=now_iso,
                )
            )

        self._cache_set(cache_id, [it.__dict__ for it in items])
        return items


def _parse_published(data: Dict[str, Any]) -> "datetime | None":
    ts = data.get("providerPublishTime") or data.get("pubDate") or data.get("published")
    if ts is None:
        return None
    # Unix seconds.
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    # ISO-8601 string.
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Yahoo RSS headline feed (keyless, published feed)
# ---------------------------------------------------------------------------

class YahooRssNewsProvider(NewsProvider):
    """News from Yahoo's RSS headline feed.

    Preferred over the private ``xhr/ncp`` endpoint that ``yfinance`` calls
    because it is a feed in a standard format rather than a private JSON API,
    it needs no API key, and it is parsed with the standard library (no added
    dependency).

    **Access-policy warning.** ``feeds.finance.yahoo.com/robots.txt`` serves
    ``User-agent: * / Disallow: /``, and this class sends a browser-like
    ``User-Agent`` because the feed answers 403 to a default client UA. The
    same is true of ``query1.finance.yahoo.com``, which is the host every
    ``yfinance`` price and fundamentals call in this project already uses --
    so this provider adds no exposure the pipeline did not already have, but
    it is not a formally sanctioned integration either. Prefer
    :class:`FinnhubNewsProvider` for unattended or shared runs: its terms
    actually permit programmatic access. Observed behaviour from datacenter
    IPs is a persistent HTTP 429 on this host while price calls still succeed,
    so treat keyless Yahoo news as best-effort only.

    Honesty notes:

    * Download failures are **raised**, not swallowed, so stage 4 can classify
      a provider outage instead of mistaking it for "this name has no news"
      (identical contract to :class:`YFinanceNewsProvider`).
    * RSS items carry no reliable per-article publisher, so ``source`` is left
      ``None`` unless the feed supplies a ``<source>`` element. That is
      deliberate: inventing a publisher would inflate the source-diversity
      term in the sentiment-confidence model.
    * The feed returns headlines plus a short description, never full article
      text -- the same limitation the sentiment layer already documents.
    """

    name = "yahoo_rss"
    FEED_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    # A browser-like UA: the feed answers 403 to the default urllib/requests UA.
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    _TIMEOUT = 20

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8),
           retry=retry_if_exception(_not_rate_limited), reraise=True)
    def _download(self, provider_symbol: str) -> List[Dict[str, Any]]:
        import xml.etree.ElementTree as ET

        import requests

        self.rate_limiter.acquire()
        resp = requests.get(
            self.FEED_URL.format(symbol=provider_symbol),
            headers=self._HEADERS,
            timeout=self._TIMEOUT,
        )
        # 404/429/5xx must surface as provider failures, not as "no news".
        _raise_for_status(resp)
        root = ET.fromstring(resp.content)

        out: List[Dict[str, Any]] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            out.append({
                "title": title,
                "description": (item.findtext("description") or "").strip() or None,
                "link": (item.findtext("link") or "").strip() or None,
                # Normalize RFC-822 to ISO-8601 so _parse_published handles it.
                "pubDate": _rfc822_to_iso(item.findtext("pubDate")),
                "publisher": (item.findtext("source") or "").strip() or None,
            })
        return out

    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        ticker = ticker.strip().upper()
        provider_symbol = to_provider_symbol(ticker, self.name)
        cache_id = f"{provider_symbol}:{max_age_days}"
        cached = self._cache_get(cache_id)
        if cached is not None:
            return [NewsItem(**row) for row in cached]

        raw = get_throttle(self.name).call(self._download, provider_symbol)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        now_iso = self._now_iso()
        items: List[NewsItem] = []
        for entry in raw:
            published_dt = _parse_published(entry)
            if published_dt is not None and published_dt < cutoff:
                continue
            items.append(
                NewsItem(
                    ticker=ticker,
                    headline=entry["title"],
                    summary=entry.get("description"),
                    source=entry.get("publisher"),
                    url=entry.get("link"),
                    published_at=published_dt.isoformat() if published_dt else None,
                    retrieved_at=now_iso,
                )
            )
        self._cache_set(cache_id, [it.__dict__ for it in items])
        return items


def _rfc822_to_iso(value: "str | None") -> "str | None":
    """RSS ``pubDate`` (RFC-822) -> ISO-8601 UTC. Naive stamps are treated as UTC."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Finnhub company news (documented API, free tier, requires a key)
# ---------------------------------------------------------------------------

class FinnhubNewsProvider(NewsProvider):
    """Company news from Finnhub's documented ``/company-news`` endpoint.

    This is the recommended news backend. Unlike the Yahoo-derived providers it
    is a published API whose terms permit programmatic access, it returns a
    real per-article ``source`` (so the source-diversity term in the
    sentiment-confidence model is meaningful rather than inflated), and it is
    not rate-limited away from datacenter IPs.

    Requires a free API key in ``FINNHUB_API_KEY``. The key is sent in the
    ``X-Finnhub-Token`` header rather than as a query parameter so it does not
    leak into URLs, logs, or cache keys.

    Honesty notes:

    * A missing key raises :class:`NewsProviderNotConfigured` **before** any
      network call. It is a configuration fault, not "no news", and it must
      never silently become neutral sentiment. In a chain the next provider
      takes over; as the sole provider the run reports news as unavailable.
    * HTTP errors (401 bad key, 403 plan limit, 429 rate limit, 5xx) are
      raised so stage 4 classifies a real outage.
    * The free tier covers US-listed companies. A covered symbol with no
      recent articles legitimately returns an empty list.
    """

    name = "finnhub"
    API_URL = "https://finnhub.io/api/v1/company-news"
    ENV_KEY = "FINNHUB_API_KEY"
    _TIMEOUT = 20

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Warn once per instance; the exception is still raised every time.
        self._warned_missing_key = False

    def _api_key(self) -> str:
        key = (os.environ.get(self.ENV_KEY) or "").strip()
        if not key:
            if not self._warned_missing_key:
                self._warned_missing_key = True
                logger.warning(
                    "news provider %r is selected but %s is not set; set it (see "
                    ".env.example) or choose a keyless provider. No news will be "
                    "fetched by this provider.",
                    self.name, self.ENV_KEY,
                )
            raise NewsProviderNotConfigured(
                f"{self.name} news provider requires {self.ENV_KEY} to be set"
            )
        return key

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8),
           retry=retry_if_exception(_not_rate_limited), reraise=True)
    def _download(self, provider_symbol: str, max_age_days: int) -> List[Dict[str, Any]]:
        import requests

        key = self._api_key()
        today = datetime.now(timezone.utc).date()
        params = {
            "symbol": provider_symbol,
            "from": (today - timedelta(days=int(max_age_days))).isoformat(),
            "to": today.isoformat(),
        }
        self.rate_limiter.acquire()
        resp = requests.get(
            self.API_URL,
            params=params,
            headers={"X-Finnhub-Token": key, "Accept": "application/json"},
            timeout=self._TIMEOUT,
        )
        # 401/403/429/5xx must surface as provider failures, not as "no news".
        _raise_for_status(resp)
        payload = resp.json()
        if isinstance(payload, dict):
            # The API reports auth/plan problems as a JSON object with "error".
            raise RuntimeError(
                f"finnhub returned an error payload: {payload.get('error') or payload}"
            )
        if not isinstance(payload, list):
            raise RuntimeError(
                f"finnhub returned {type(payload).__name__}, expected a JSON array"
            )
        return [row for row in payload if isinstance(row, dict)]

    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        ticker = ticker.strip().upper()
        provider_symbol = to_provider_symbol(ticker, self.name)
        cache_id = f"{provider_symbol}:{max_age_days}"
        cached = self._cache_get(cache_id)
        if cached is not None:
            return [NewsItem(**row) for row in cached]

        raw = get_throttle(self.name).call(self._download, provider_symbol, max_age_days)

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        now_iso = self._now_iso()
        items: List[NewsItem] = []
        for entry in raw:
            headline = str(entry.get("headline") or "").strip()
            if not headline:
                continue
            # Finnhub stamps articles with unix seconds under "datetime".
            published_dt = _parse_published({"providerPublishTime": entry.get("datetime")})
            if published_dt is not None and published_dt < cutoff:
                continue
            summary = str(entry.get("summary") or "").strip() or None
            source = str(entry.get("source") or "").strip() or None
            url = str(entry.get("url") or "").strip() or None
            items.append(
                NewsItem(
                    ticker=ticker,
                    headline=headline,
                    summary=summary,
                    source=source,
                    url=url,
                    published_at=published_dt.isoformat() if published_dt else None,
                    retrieved_at=now_iso,
                )
            )
        self._cache_set(cache_id, [it.__dict__ for it in items])
        return items



def _raise_for_status(resp: Any) -> None:
    """Like ``raise_for_status``, but a 429 carries the server's ``Retry-After``.

    The URL is deliberately left out of the 429 message: for Finnhub it would
    be harmless (the key travels in a header), but no provider's URL needs to
    be in a log line to explain a rate limit.
    """
    if getattr(resp, "status_code", 200) == 429:
        import requests

        headers = getattr(resp, "headers", None) or {}
        exc = requests.HTTPError("429 Client Error: Too Many Requests", response=resp)
        exc.retry_after = parse_retry_after(headers.get("Retry-After"))  # type: ignore[attr-defined]
        raise exc
    resp.raise_for_status()
