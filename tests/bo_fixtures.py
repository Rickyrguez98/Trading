"""Shared synthetic-data fixtures for the binary-options tests.

Everything here is deterministic and offline: no network, no real provider.
``NOW`` is a Wednesday 11:00 ET (15:00 UTC, EDT) so the regular session is
open unless a test says otherwise.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from binary_options_selection.config import BinaryOptionsConfig, load_config
from binary_options_selection.data_providers.base import (
    Candle,
    IntradayCandlesProvider,
    IntradaySeries,
)
from binary_options_selection.utils.timeutils import candle_close_time, iso_utc

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)  # Wed 2026-07-15 11:00 ET
INTERVAL_MIN = 5
INTERVAL_SEC = 300


def make_config(**overrides) -> BinaryOptionsConfig:
    cfg = load_config({})  # dataclass defaults; validated
    cfg.cache.enabled = False
    cfg.robustness.run_health_check = False
    for dotted, value in overrides.items():
        section, attr = dotted.split(".", 1)
        setattr(getattr(cfg, section), attr, value)
    return cfg


def make_candles(
    n: int = 120,
    *,
    end: datetime = NOW,
    base: float = 100.0,
    step: Callable[[int, float], float] = lambda i, p: 0.0,
    volume: Callable[[int], float] = lambda i: 50_000.0,
) -> List[Candle]:
    """Build ``n`` completed 5-minute candles ending exactly at ``end``.

    ``step(i, price)`` returns the ADDITIVE price change of bar ``i`` (linear
    paths avoid the geometric up/down asymmetry of percentage steps).
    """
    candles: List[Candle] = []
    price = base
    for i in range(n):
        start = end - timedelta(seconds=INTERVAL_SEC * (n - i))
        o = price
        price = max(0.01, price + step(i, price))
        c = price
        hi = max(o, c) * 1.0005
        lo = min(o, c) * 0.9995
        candles.append(Candle(start=start, open=o, high=hi, low=lo, close=c,
                              volume=volume(i), complete=True))
    return candles


def uptrend(i: int, p: float) -> float:
    return 0.25 if i % 4 else -0.05     # rises with small pullbacks


def downtrend(i: int, p: float) -> float:
    return -0.25 if i % 4 else 0.05     # falls with small bounces


def flat(i: int, p: float) -> float:
    return 0.03 if i % 2 else -0.03     # oscillates around the base


def trend_volume(i: int) -> float:
    """Higher volume on trend bars (i % 4 != 0) so the last bar of an
    up/downtrend carries genuine volume confirmation."""
    return 90_000.0 if i % 4 else 30_000.0


def make_series(
    symbol: str,
    candles: List[Candle],
    *,
    status: str = "ok",
    error_type: Optional[str] = None,
    provider: str = "yfinance",  # ScriptedProvider stands in for the default
) -> IntradaySeries:
    s = IntradaySeries(
        symbol=symbol, provider_symbol=symbol.replace(".", "-"),
        interval="5m", interval_seconds=INTERVAL_SEC,
        candles=list(candles), provider=provider,
        status=status, error_type=error_type,
        data_source="live" if status == "ok" else "unavailable",
        requested_at=iso_utc(NOW),
    )
    if candles:
        s.latest_candle_start = iso_utc(candles[-1].start)
        s.latest_candle_close = iso_utc(candle_close_time(candles[-1].start, INTERVAL_SEC))
    return s


class ScriptedProvider(IntradayCandlesProvider):
    """Deterministic in-memory provider: symbol -> IntradaySeries factory."""

    name = "yfinance"  # stands in for the default so config validation passes

    def __init__(self, scripts: Dict[str, Callable[[], IntradaySeries]]) -> None:
        super().__init__()
        self.scripts = scripts
        self.calls: List[str] = []

    def fetch(self, symbol, *, interval, bars, include_extended_hours=False,
              ttl_seconds=None) -> IntradaySeries:
        self.calls.append(symbol)
        if symbol in self.scripts:
            return self.scripts[symbol]()
        return make_series(symbol, [], status="empty", error_type="NO_INTRADAY_DATA")
