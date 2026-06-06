"""News via yfinance's per-ticker news endpoint.

yfinance returns a list of dicts with keys like ``title``, ``publisher``,
``link``, ``providerPublishTime`` (unix seconds), ``summary`` (sometimes).
Depth and freshness vary a lot per ticker — we cap by ``max_age_days``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from tenacity import retry, stop_after_attempt, wait_exponential

from .base import NewsItem, NewsProvider
from .symbols import to_provider_symbol

logger = logging.getLogger(__name__)


class YFinanceNewsProvider(NewsProvider):
    name = "yfinance"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=False)
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

        try:
            raw = self._download(provider_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "yfinance news fetch failed for %s (as %s): %s",
                ticker, provider_symbol, exc,
            )
            raw = []

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
