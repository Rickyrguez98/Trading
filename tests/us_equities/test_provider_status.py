"""Provider honesty fields: status / provider_symbol / error.

These defaults are what let the pipeline distinguish a genuine provider miss
(empty/error) from an illiquid name we drop on a filter, and what keeps OLD
cache entries -- written before these fields existed -- reconstructable.
"""
from __future__ import annotations

from asset_selection.data_providers.base import Fundamentals, PriceSnapshot


def test_new_records_default_to_ok_status():
    snap = PriceSnapshot(ticker="AAPL")
    fund = Fundamentals(ticker="AAPL")
    assert snap.status == "ok" and snap.error is None
    assert fund.status == "ok" and fund.error is None
    # provider_symbol is optional and starts unset.
    assert snap.provider_symbol is None
    assert fund.provider_symbol is None


def test_legacy_cache_dict_reconstructs_without_new_fields():
    # Simulate a cache entry written before status/provider_symbol/error existed.
    legacy = {
        "ticker": "AAPL",
        "last_close": 200.0,
        "avg_daily_volume": 5e7,
        "avg_dollar_volume": 1e10,
        "return_pct": 0.12,
        "volatility_pct": 0.25,
        "lookback_days": 90,
        "source": "yfinance",
    }
    snap = PriceSnapshot(**legacy)  # must not raise
    assert snap.ticker == "AAPL"
    # Missing fields fall back to honest defaults.
    assert snap.status == "ok"
    assert snap.provider_symbol is None
    assert snap.error is None


def test_status_round_trips_through_dict():
    snap = PriceSnapshot(
        ticker="BRK.B", provider_symbol="BRK-B",
        status="empty", error="no data returned for BRK-B",
    )
    rebuilt = PriceSnapshot(**snap.__dict__)
    assert rebuilt.provider_symbol == "BRK-B"
    assert rebuilt.status == "empty"
    assert rebuilt.error == "no data returned for BRK-B"
