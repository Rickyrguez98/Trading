"""Provider fallback wrappers implementing the backup-plan ladder.

For each data type we try providers in priority order and then, if every live
provider fails, optionally fall back to a fresh-enough cache entry:

    Plan A  primary provider (live)
    Plan B  secondary provider(s) (live)
    Plan C  fresh-enough cache (within max_cache_age), labeled "stale_cache"
    Plan D  honest failure record (no data) -- caller decides whether to rank

The wrappers honour the same ``fetch`` contracts as the underlying providers, so
the pipeline is unaware it is talking to a chain. Provenance is recorded on each
returned record via ``data_source`` ("live" / "fallback" / "stale_cache" /
"unavailable"), and the wrapper keeps aggregate counters for a
``fallback_usage_summary`` in the run report.

Prices/fundamentals providers never raise (they return a record with a status),
so the chain inspects ``status``. The news provider *does* raise on a real
provider error (so a systemic news outage is visible), so the news chain treats
"returned a list" (even empty) as success and re-raises only if every provider
errored.
"""
from __future__ import annotations

import logging
from dataclasses import fields as _dc_fields
from typing import Any, Callable, Dict, List, Optional

from ..utils.cache import Cache
from .base import (
    Fundamentals,
    FundamentalsProvider,
    NewsItem,
    NewsProvider,
    PriceSnapshot,
    PricesProvider,
)

logger = logging.getLogger(__name__)

_PRICE_FIELDS = {f.name for f in _dc_fields(PriceSnapshot)}
_FUND_FIELDS = {f.name for f in _dc_fields(Fundamentals)}


class _FallbackMixin:
    """Shared chain logic + usage counters for record-returning providers."""

    def __init__(
        self,
        providers: List[Any],
        *,
        use_cache_on_failure: bool = False,
        max_cache_age_seconds: int = 7 * 86400,
    ) -> None:
        if not providers:
            raise ValueError("FallbackProvider requires at least one provider.")
        self.providers = providers
        self.use_cache_on_failure = use_cache_on_failure
        self.max_cache_age_seconds = max_cache_age_seconds
        self.usage: Dict[str, Any] = {
            "primary": 0,
            "fallback": 0,
            "stale_cache": 0,
            "unavailable": 0,
            "by_provider": {},
        }

    # Names of the chain, primary first.
    @property
    def provider_names(self) -> List[str]:
        return [getattr(p, "name", "?") for p in self.providers]

    def _bump(self, kind: str, provider_name: Optional[str] = None) -> None:
        self.usage[kind] = self.usage.get(kind, 0) + 1
        if provider_name:
            self.usage["by_provider"][provider_name] = (
                self.usage["by_provider"].get(provider_name, 0) + 1
            )

    def _run_chain(
        self,
        *,
        call: Callable[[Any], Any],
        is_usable: Callable[[Any], bool],
        reconstruct: Callable[[Dict[str, Any]], Any],
        cache_id_for: Callable[[Any], str],
    ) -> Any:
        last = None
        # Accumulate the per-provider attempt trail across the WHOLE chain so the
        # surviving record shows that (e.g.) yfinance AND Stooq were tried, with
        # the symbol variant + result for each. Previously the chain returned
        # only the last provider's record, which made a Stooq ``nvda.us`` failure
        # read as if yfinance had tried ``nvda.us`` -- the core reporting bug.
        prior_attempts: List[Dict[str, Any]] = []
        for idx, provider in enumerate(self.providers):
            rec = call(provider)
            last = rec
            rec_attempts = list(getattr(rec, "provider_attempts", None) or [])
            if is_usable(rec):
                kind = "primary" if idx == 0 else "fallback"
                rec.data_source = "live" if idx == 0 else "fallback"
                self._bump(kind, getattr(provider, "name", None))
                if prior_attempts and hasattr(rec, "provider_attempts"):
                    rec.provider_attempts = prior_attempts + rec_attempts
                return rec
            # This provider failed -> keep its attempts for the eventual record.
            prior_attempts.extend(rec_attempts)

        # Plan C: fresh-enough cache from the primary provider.
        if self.use_cache_on_failure:
            primary = self.providers[0]
            cache = getattr(primary, "cache", None)
            if isinstance(cache, Cache):
                entry = cache.get_entry(
                    primary.cache_namespace, cache_id_for(primary),
                    max_age_seconds=self.max_cache_age_seconds,
                )
                if entry is not None and entry.payload:
                    rec = reconstruct(entry.payload)
                    if is_usable(rec):
                        rec.data_source = "stale_cache"
                        self._bump("stale_cache", getattr(primary, "name", None))
                        logger.info(
                            "Served stale cache (age ~%.0fs) after live providers failed.",
                            __import__("time").time() - entry.timestamp,
                        )
                        if prior_attempts and hasattr(rec, "provider_attempts"):
                            rec.provider_attempts = prior_attempts + list(
                                getattr(rec, "provider_attempts", None) or []
                            )
                        return rec

        # Plan D: honest failure. The final record carries the full chain trail.
        self._bump("unavailable")
        if last is not None:
            last.data_source = "unavailable"
            if hasattr(last, "provider_attempts"):
                last.provider_attempts = prior_attempts
        return last


class FallbackPricesProvider(_FallbackMixin, PricesProvider):
    name = "fallback-prices"

    def __init__(self, providers, **kw):
        _FallbackMixin.__init__(self, providers, **kw)
        # Inherit the primary's cache namespace/ttl behaviour for any base calls.
        PricesProvider.__init__(self, cache=getattr(providers[0], "cache", None))

    def cache_identifier(self, ticker: str, lookback_days: int = 90) -> str:
        return self.providers[0].cache_identifier(ticker, lookback_days)

    def fetch(self, ticker: str, lookback_days: int = 90) -> PriceSnapshot:
        return self._run_chain(
            call=lambda p: p.fetch(ticker, lookback_days=lookback_days),
            is_usable=lambda r: getattr(r, "status", "ok") == "ok"
            and getattr(r, "last_close", None) is not None,
            reconstruct=lambda payload: PriceSnapshot(
                **{k: v for k, v in payload.items() if k in _PRICE_FIELDS}
            ),
            cache_id_for=lambda p: p.cache_identifier(ticker, lookback_days),
        )


class FallbackFundamentalsProvider(_FallbackMixin, FundamentalsProvider):
    name = "fallback-fundamentals"

    def __init__(self, providers, **kw):
        _FallbackMixin.__init__(self, providers, **kw)
        FundamentalsProvider.__init__(self, cache=getattr(providers[0], "cache", None))

    def cache_identifier(self, ticker: str) -> str:
        return self.providers[0].cache_identifier(ticker)

    def fetch(self, ticker: str) -> Fundamentals:
        return self._run_chain(
            call=lambda p: p.fetch(ticker),
            is_usable=lambda r: getattr(r, "status", "ok") == "ok",
            reconstruct=lambda payload: Fundamentals(
                **{k: v for k, v in payload.items() if k in _FUND_FIELDS}
            ),
            cache_id_for=lambda p: p.cache_identifier(ticker),
        )


class FallbackNewsProvider(_FallbackMixin, NewsProvider):
    name = "fallback-news"

    def __init__(self, providers, **kw):
        _FallbackMixin.__init__(self, providers, **kw)
        NewsProvider.__init__(self, cache=getattr(providers[0], "cache", None))

    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        last_exc: Optional[BaseException] = None
        for idx, provider in enumerate(self.providers):
            try:
                items = provider.fetch(ticker, max_age_days=max_age_days)
            except Exception as exc:  # noqa: BLE001 - try the next provider
                last_exc = exc
                continue
            # A successful call (even an empty list -- genuinely no coverage)
            # ends the chain. Empty != provider failure for news.
            self._bump("primary" if idx == 0 else "fallback", getattr(provider, "name", None))
            return items
        # Every provider errored -> re-raise so stage 4 classifies it as a
        # systemic news failure rather than silently neutral sentiment.
        self._bump("unavailable")
        if last_exc is not None:
            raise last_exc
        return []
