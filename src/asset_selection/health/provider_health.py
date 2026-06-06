"""Probe providers against benchmark mega-caps to detect systemic failures.

The core idea: AAPL, MSFT, and GOOGL are about as liquid and well-covered as
equities get. If a price provider cannot return data for *any* of them, the
provider is down/blocked/rate-limited -- a **systemic** failure -- and the
pipeline must not pretend a ranking built off the survivors is trustworthy.

Each probe yields a :class:`HealthCheckResult` with the exact fields the audit
asked for (provider_name, data_type, ticker, canonical/provider symbol,
success, error_type, error_message, response_summary, timestamp). The module is
provider-agnostic: callers pass in already-constructed provider objects, so it
is fully testable with fakes and never touches the network itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..data_providers import errors as err
from ..data_providers.symbols import to_provider_symbol

logger = logging.getLogger(__name__)

# Benchmark set required by the audit. BRK.B doubles as a class-share
# normalization probe (canonical BRK.B -> provider BRK-B).
BENCHMARK_TICKERS: List[str] = ["AAPL", "MSFT", "GOOGL", "NVDA", "BRK.B"]

# The mega-caps whose *collective* price failure proves a systemic outage.
# (NVDA/BRK.B can legitimately vary by provider coverage; AAPL/MSFT/GOOGL not.)
_SYSTEMIC_PRICE_BELLWETHERS = ("AAPL", "MSFT", "GOOGL")

DATA_TYPES = ("price", "fundamentals", "news")


@dataclass
class HealthCheckResult:
    provider_name: str
    data_type: str               # "price" | "fundamentals" | "news"
    ticker: str                  # canonical symbol (e.g. BRK.B)
    canonical_symbol: str
    provider_symbol: str         # what we actually sent (e.g. BRK-B)
    success: bool
    error_type: str              # errors.* constant; OK on success
    error_message: Optional[str] = None
    response_summary: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "data_type": self.data_type,
            "ticker": self.ticker,
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "success": self.success,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "response_summary": self.response_summary,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Per-data-type probes
# ---------------------------------------------------------------------------

def _error_type_from_record(record: Any, data_type: str) -> str:
    """Derive an error-type constant from a returned provider record.

    Prefers a record-native ``error_type`` (added in the classification commit);
    otherwise falls back to the coarse ``status`` + ``error`` text.
    """
    et = getattr(record, "error_type", None)
    if et:
        return et
    status = getattr(record, "status", "ok")
    if status == "ok":
        return err.OK
    if status == "empty":
        return err.classify_empty(data_type)
    return err.classify_error_text(getattr(record, "error", None))


def _probe_price(provider, ticker: str, provider_name: str) -> HealthCheckResult:
    canonical = ticker.strip().upper()
    provider_symbol = to_provider_symbol(canonical, provider_name)
    try:
        snap = provider.fetch(canonical, lookback_days=90)
    except Exception as exc:  # noqa: BLE001 - report-and-continue
        return HealthCheckResult(
            provider_name, "price", canonical, canonical, provider_symbol,
            success=False, error_type=err.classify_exception(exc),
            error_message=f"{type(exc).__name__}: {exc}",
        )
    status = getattr(snap, "status", "ok")
    has_price = getattr(snap, "last_close", None) is not None
    success = status == "ok" and has_price
    summary = (
        f"last_close={getattr(snap, 'last_close', None)}, "
        f"adv={getattr(snap, 'avg_dollar_volume', None)}"
        if success else None
    )
    return HealthCheckResult(
        provider_name, "price", canonical, canonical,
        getattr(snap, "provider_symbol", provider_symbol),
        success=success,
        error_type=err.OK if success else _error_type_from_record(snap, "price"),
        error_message=None if success else getattr(snap, "error", None),
        response_summary=summary,
    )


def _probe_fundamentals(provider, ticker: str, provider_name: str) -> HealthCheckResult:
    canonical = ticker.strip().upper()
    provider_symbol = to_provider_symbol(canonical, provider_name)
    try:
        rec = provider.fetch(canonical)
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            provider_name, "fundamentals", canonical, canonical, provider_symbol,
            success=False, error_type=err.classify_exception(exc),
            error_message=f"{type(exc).__name__}: {exc}",
        )
    status = getattr(rec, "status", "ok")
    # "Success" for fundamentals = the call worked AND at least one substantive
    # field came back (market cap or a profitability/identity field). An empty
    # blob with status ok still counts as a miss for health purposes.
    substantive = any(
        getattr(rec, f, None) is not None
        for f in ("market_cap", "company_name", "pe_ratio", "roe", "net_margin")
    )
    success = status == "ok" and substantive
    summary = (
        f"market_cap={getattr(rec, 'market_cap', None)}, "
        f"name={getattr(rec, 'company_name', None)}"
        if success else None
    )
    error_type = err.OK
    if not success:
        error_type = (
            _error_type_from_record(rec, "fundamentals")
            if status != "ok" else err.NO_FUNDAMENTAL_DATA
        )
    return HealthCheckResult(
        provider_name, "fundamentals", canonical, canonical,
        getattr(rec, "provider_symbol", provider_symbol),
        success=success, error_type=error_type,
        error_message=None if success else getattr(rec, "error", None),
        response_summary=summary,
    )


def _probe_news(provider, ticker: str, provider_name: str) -> HealthCheckResult:
    canonical = ticker.strip().upper()
    provider_symbol = to_provider_symbol(canonical, provider_name)
    try:
        items = provider.fetch(canonical, max_age_days=30)
    except Exception as exc:  # noqa: BLE001
        return HealthCheckResult(
            provider_name, "news", canonical, canonical, provider_symbol,
            success=False, error_type=err.classify_exception(exc),
            error_message=f"{type(exc).__name__}: {exc}",
        )
    n = len(items or [])
    success = n > 0
    return HealthCheckResult(
        provider_name, "news", canonical, canonical, provider_symbol,
        success=success,
        # An empty news list is NOT a provider failure on its own (a name can
        # simply have no recent coverage); we record it as NO_NEWS_DATA so the
        # systemic classifier can weigh it, but it never blocks ranking.
        error_type=err.OK if success else err.NO_NEWS_DATA,
        error_message=None,
        response_summary=f"{n} article(s)" if success else "0 articles",
    )


_PROBES = {
    "price": _probe_price,
    "fundamentals": _probe_fundamentals,
    "news": _probe_news,
}


# ---------------------------------------------------------------------------
# Orchestration + systemic classification
# ---------------------------------------------------------------------------

def run_provider_health_checks(
    *,
    price_provider=None,
    fundamentals_provider=None,
    news_provider=None,
    tickers: Sequence[str] = BENCHMARK_TICKERS,
    provider_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Probe the supplied providers across ``tickers`` and classify health.

    Any provider left as ``None`` is skipped (its data type is reported as
    "unknown"). Returns a structured dict (see :func:`summarize_health`).
    """
    names = {"price": "prices", "fundamentals": "fundamentals", "news": "news"}
    names.update(provider_names or {})
    providers = {
        "price": price_provider,
        "fundamentals": fundamentals_provider,
        "news": news_provider,
    }

    results: List[HealthCheckResult] = []
    for data_type, provider in providers.items():
        if provider is None:
            continue
        pname = getattr(provider, "name", names.get(data_type, data_type))
        probe = _PROBES[data_type]
        for ticker in tickers:
            res = probe(provider, ticker, pname)
            results.append(res)
            logger.info(
                "Health %-12s %-5s %-6s -> %s%s",
                data_type, ticker, "ok" if res.success else "FAIL",
                res.error_type,
                "" if res.success else f" ({res.error_message or ''})",
            )
    return summarize_health(results, tickers=list(tickers))


def summarize_health(
    results: Sequence[HealthCheckResult],
    tickers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Aggregate per-check results into per-data-type health + overall verdict."""
    by_type: Dict[str, Dict[str, Any]] = {}
    for dt in DATA_TYPES:
        dt_results = [r for r in results if r.data_type == dt]
        if not dt_results:
            continue
        n = len(dt_results)
        ok = sum(1 for r in dt_results if r.success)
        provider_side = sum(1 for r in dt_results if err.is_provider_side(r.error_type))
        success_ratio = ok / n if n else 0.0

        systemic = _is_systemic(dt, dt_results, provider_side, n)
        by_type[dt] = {
            "provider_name": dt_results[0].provider_name,
            "checked": n,
            "succeeded": ok,
            "failed": n - ok,
            "success_ratio": round(success_ratio, 3),
            "provider_side_failures": provider_side,
            "systemic_failure": systemic,
            "error_types": _count_error_types(dt_results),
            "results": [r.to_dict() for r in dt_results],
        }

    # Overall: any systemic failure on price/fundamentals is decisive. News is
    # advisory only (it degrades sentiment confidence, never blocks ranking).
    price_systemic = by_type.get("price", {}).get("systemic_failure", False)
    fund_systemic = by_type.get("fundamentals", {}).get("systemic_failure", False)
    any_blocking_systemic = bool(price_systemic or fund_systemic)

    if any_blocking_systemic:
        overall = "systemic_failure"
    elif by_type and all(v["success_ratio"] >= 0.999 for v in by_type.values()):
        overall = "healthy"
    elif by_type:
        overall = "degraded"
    else:
        overall = "unknown"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_tickers": list(tickers or BENCHMARK_TICKERS),
        "overall_status": overall,
        "price_systemic_failure": price_systemic,
        "fundamentals_systemic_failure": fund_systemic,
        "any_blocking_systemic_failure": any_blocking_systemic,
        "by_data_type": by_type,
    }


def _is_systemic(data_type: str, dt_results, provider_side: int, n: int) -> bool:
    """Decide whether a data type's failures look systemic (provider down).

    Price: systemic if the AAPL/MSFT/GOOGL bellwethers all failed, OR if every
    probe was a provider-side error. Fundamentals: systemic if every probe was a
    provider-side error (an empty fundamentals blob for one odd name is not
    systemic). News is never treated as systemic-blocking.
    """
    if data_type == "news":
        return False
    if n and provider_side == n:
        return True
    if data_type == "price":
        bellwether = [r for r in dt_results if r.ticker in _SYSTEMIC_PRICE_BELLWETHERS]
        if bellwether and all(not r.success for r in bellwether):
            return True
    return False


def _count_error_types(dt_results) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in dt_results:
        if r.success:
            continue
        out[r.error_type] = out.get(r.error_type, 0) + 1
    return out
