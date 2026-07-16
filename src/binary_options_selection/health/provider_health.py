"""Intraday provider health probe.

Fetches a small candle window for the configured benchmark symbols and
classifies the outcome, so a provider outage is caught before it can masquerade
as "no signals today". Mirrors the equity module's benchmark health check.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from ..config import BinaryOptionsConfig
from ..data_providers import errors as err
from ..data_providers.base import IntradayCandlesProvider

logger = logging.getLogger(__name__)


@dataclass
class IntradayHealthResult:
    symbol: str
    provider: str
    ok: bool
    candle_count: int = 0
    status: str = "ok"
    error_type: str = err.OK
    error: str = ""
    latest_candle_close: str = ""


@dataclass
class IntradayHealthSummary:
    provider: str
    results: List[IntradayHealthResult] = field(default_factory=list)
    ok_count: int = 0
    fail_count: int = 0
    provider_side_failures: int = 0
    systemic_failure: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def run_intraday_health_check(
    provider: IntradayCandlesProvider,
    cfg: BinaryOptionsConfig,
) -> IntradayHealthSummary:
    summary = IntradayHealthSummary(provider=provider.name)
    ttl = cfg.cache.ttl_for_interval(cfg.candles.interval_seconds)
    for symbol in cfg.robustness.health_check_symbols:
        series = provider.fetch(
            symbol,
            interval=cfg.candles.interval,
            bars=min(20, cfg.candles.fetch_history_bars),
            include_extended_hours=False,
            ttl_seconds=ttl,
        )
        ok = series.status == "ok" and bool(series.candles)
        result = IntradayHealthResult(
            symbol=symbol,
            provider=provider.name,
            ok=ok,
            candle_count=len(series.candles),
            status=series.status,
            error_type=series.error_type or err.OK,
            error=series.error or "",
            latest_candle_close=series.latest_candle_close or "",
        )
        summary.results.append(result)
        if ok:
            summary.ok_count += 1
        else:
            summary.fail_count += 1
            if err.is_provider_side(series.error_type):
                summary.provider_side_failures += 1
        logger.info(
            "Intraday health %-6s %s (%d candles)%s",
            symbol, "OK" if ok else "FAIL", len(series.candles),
            "" if ok else f" -> {series.error_type}",
        )
    total = len(summary.results)
    # Systemic = every benchmark failed and at least one failure was
    # provider-side (transport), not just an empty weekend window.
    summary.systemic_failure = (
        total > 0
        and summary.ok_count == 0
        and summary.provider_side_failures > 0
    )
    return summary
