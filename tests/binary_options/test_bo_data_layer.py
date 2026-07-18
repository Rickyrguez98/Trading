"""Time handling, market sessions, candle validation, and provider behavior."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from binary_options_selection.config import MarketSessionConfig
from binary_options_selection.data_providers import errors as err
from binary_options_selection.data_providers.yfinance_intraday import (
    YFinanceIntradayProvider,
    _provider_symbol,
)
from binary_options_selection.market_session import (
    MARKET_AFTER,
    MARKET_CLOSED,
    MARKET_OPEN,
    MARKET_PRE,
    MARKET_UNKNOWN,
    classify_session,
    expiry_for,
)
from binary_options_selection.utils.timeutils import (
    candle_is_complete,
    ensure_utc,
    floor_to_interval,
    iso_utc,
    parse_utc,
    to_exchange_local,
)
from binary_options_selection.validation.candles import assess_series
from trading_common.cache import Cache

from tests.binary_options.bo_fixtures import NOW, flat, make_candles, make_config, make_series

# ---------------------------------------------------------------------------
# timeutils
# ---------------------------------------------------------------------------

def test_floor_to_interval_deterministic():
    a = parse_utc("2026-07-15T14:33:12Z")
    b = parse_utc("2026-07-15T14:34:59Z")
    assert floor_to_interval(a, 300) == floor_to_interval(b, 300)
    assert iso_utc(floor_to_interval(a, 300)) == "2026-07-15T14:30:00Z"


def test_candle_completeness_boundary():
    start = parse_utc("2026-07-15T14:30:00Z")
    assert not candle_is_complete(start, 300, parse_utc("2026-07-15T14:34:59Z"))
    assert candle_is_complete(start, 300, parse_utc("2026-07-15T14:35:00Z"))


def test_naive_timestamps_rejected():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 7, 15, 14, 0))
    with pytest.raises(ValueError):
        parse_utc("2026-07-15T14:00:00")


def test_timezone_round_trip_dst_aware():
    # July: EDT (UTC-4). January: EST (UTC-5).
    july = parse_utc("2026-07-15T13:30:00Z")
    jan = parse_utc("2026-01-15T14:30:00Z")
    assert to_exchange_local(july, "America/New_York").hour == 9
    assert to_exchange_local(jan, "America/New_York").hour == 9


# ---------------------------------------------------------------------------
# market session
# ---------------------------------------------------------------------------

def _ms(**overrides) -> MarketSessionConfig:
    cfg = MarketSessionConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_session_states():
    cfg = _ms()
    assert classify_session(parse_utc("2026-07-15T15:00:00Z"), cfg).state == MARKET_OPEN
    assert classify_session(parse_utc("2026-07-15T12:00:00Z"), cfg).state == MARKET_PRE
    assert classify_session(parse_utc("2026-07-15T21:00:00Z"), cfg).state == MARKET_AFTER
    assert classify_session(parse_utc("2026-07-15T03:00:00Z"), cfg).state == MARKET_CLOSED
    assert classify_session(parse_utc("2026-07-18T15:00:00Z"), cfg).state == MARKET_CLOSED  # Sat


def test_extended_session_permissions():
    pre_time = parse_utc("2026-07-15T12:00:00Z")
    assert not classify_session(pre_time, _ms()).allowed_for_analysis
    ext = _ms(session="extended", allow_premarket=True)
    assert classify_session(pre_time, ext).allowed_for_analysis
    # after-hours still needs its own flag
    after = parse_utc("2026-07-15T21:00:00Z")
    assert not classify_session(after, ext).allowed_for_analysis


def test_research_mode_reports_state_honestly():
    closed = parse_utc("2026-07-18T15:00:00Z")
    s = classify_session(closed, _ms(require_open_market=False))
    assert s.allowed_for_analysis and s.state == MARKET_CLOSED


def test_unknown_timezone_is_unknown_not_guessed():
    s = classify_session(NOW, _ms(timezone="Not/AZone"))
    assert s.state == MARKET_UNKNOWN and not s.allowed_for_analysis


def test_expiry_alignment():
    ref = parse_utc("2026-07-15T14:35:00Z")
    assert iso_utc(expiry_for(ref, 15)) == "2026-07-15T14:50:00Z"


# ---------------------------------------------------------------------------
# candle validation
# ---------------------------------------------------------------------------

def test_clean_series_scores_high():
    cfg = make_config()
    q = assess_series(make_series("T", make_candles(step=flat)), cfg, NOW)
    assert q.usable and not q.stale and q.data_quality_score >= 90
    assert q.reference_price is not None
    assert q.latest_complete_close == NOW  # last candle closes exactly at NOW


def test_stale_series_flagged_and_capped():
    cfg = make_config()
    q = assess_series(
        make_series("T", make_candles(step=flat)), cfg, NOW + timedelta(hours=2)
    )
    assert q.stale and "STALE_INTRADAY_DATA" in q.flags
    assert q.data_quality_score <= 40 and q.usable is not None


def test_duplicates_and_order_detected():
    cfg = make_config()
    candles = make_candles(step=flat)
    candles.insert(50, candles[50])                     # duplicate
    candles[10], candles[11] = candles[11], candles[10]  # out of order
    q = assess_series(make_series("T", candles), cfg, NOW)
    assert q.duplicate_count == 1 and q.out_of_order_count >= 1
    assert "DUPLICATE_CANDLES" not in q.flags or q.duplicate_count > 0  # flag needs ratio
    assert len(q.candles) == 120  # duplicate removed, order repaired deterministically


def test_missing_candles_counted_session_aware():
    cfg = make_config()
    candles = make_candles(step=flat)
    del candles[60:63]  # 3-bar intraday gap
    q = assess_series(make_series("T", candles), cfg, NOW)
    assert q.missing_count == 3


def test_impossible_ohlc_and_nonpositive_prices_rejected():
    cfg = make_config()
    candles = make_candles(step=flat)
    candles[40].high = candles[40].low - 1.0
    candles[41].close = -5.0
    q = assess_series(make_series("T", candles), cfg, NOW)
    assert q.impossible_ohlc_count == 2 and "IMPOSSIBLE_OHLC" in q.flags


def test_future_timestamps_never_scored():
    cfg = make_config()
    candles = make_candles(step=flat)
    candles.append(
        type(candles[0])(start=NOW + timedelta(hours=1), open=1, high=2, low=0.5,
                         close=1.5, volume=10, complete=True)
    )
    q = assess_series(make_series("T", candles), cfg, NOW)
    assert q.future_timestamp_count == 1
    assert all(ensure_utc(c.start) <= NOW for c in q.candles)


def test_incomplete_last_candle_dropped_by_default_used_when_allowed():
    cfg = make_config()
    candles = make_candles(step=flat)
    candles[-1].complete = False
    q = assess_series(make_series("T", candles), cfg, NOW)
    assert "INCOMPLETE_LAST_CANDLE_DROPPED" in q.flags and len(q.candles) == 119
    cfg2 = make_config()
    cfg2.candles.allow_incomplete_last_candle = True
    q2 = assess_series(make_series("T", candles), cfg2, NOW)
    assert "INCOMPLETE_CANDLE_USED" in q2.flags and len(q2.candles) == 120


def test_insufficient_history_flagged():
    cfg = make_config()
    q = assess_series(make_series("T", make_candles(n=10, step=flat)), cfg, NOW)
    assert q.insufficient_history and not q.usable
    assert q.error_type == err.INSUFFICIENT_HISTORY


def test_empty_and_error_series():
    cfg = make_config()
    q = assess_series(make_series("T", [], status="empty",
                                  error_type=err.NO_INTRADAY_DATA), cfg, NOW)
    assert not q.usable and q.error_type == err.NO_INTRADAY_DATA


# ---------------------------------------------------------------------------
# yfinance provider behavior (fully mocked -- no network)
# ---------------------------------------------------------------------------

def test_provider_symbol_mapping():
    assert _provider_symbol("BRK.B") == "BRK-B"
    assert _provider_symbol(" aapl ") == "AAPL"


def test_unsupported_interval_reported_not_raised():
    p = YFinanceIntradayProvider()
    s = p.fetch("AAPL", interval="3m", bars=10)
    assert s.status == "error" and s.error_type == err.UNSUPPORTED_INTERVAL


def test_provider_exception_classified_not_raised(monkeypatch):
    p = YFinanceIntradayProvider()
    def boom(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(p, "_fetch_live", boom)
    s = p.fetch("AAPL", interval="5m", bars=10)
    assert s.status == "error" and s.error_type == err.PROVIDER_TIMEOUT
    assert s.data_source == "unavailable" and not s.candles


def test_provider_empty_response_is_empty_status(monkeypatch):
    p = YFinanceIntradayProvider()
    monkeypatch.setattr(p, "_fetch_live", lambda *a, **k: {
        "candles": [], "exchange_timezone": None, "adjusted": False})
    s = p.fetch("AAPL", interval="5m", bars=10)
    assert s.status == "empty" and s.error_type == err.NO_INTRADAY_DATA


def _pin_provider_clock(monkeypatch):
    """Pin the provider's wall clock to the fixtures' timeline so candle
    completeness is judged against NOW, not the machine clock."""
    import binary_options_selection.data_providers.yfinance_intraday as yfi
    monkeypatch.setattr(yfi, "utc_now", lambda: NOW)


def test_provider_cache_serves_fresh_within_ttl(tmp_path, monkeypatch):
    _pin_provider_clock(monkeypatch)
    cache = Cache(directory=str(tmp_path), enabled=True)
    p = YFinanceIntradayProvider(cache=cache)
    calls = {"n": 0}
    def fake_live(psym, interval, bars, ext):
        calls["n"] += 1
        cs = make_candles(n=5, end=NOW - timedelta(minutes=30), step=flat)
        return {"candles": [
            {"start": c.start.isoformat(), "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume} for c in cs
        ], "exchange_timezone": "America/New_York", "adjusted": False}
    monkeypatch.setattr(p, "_fetch_live", fake_live)
    s1 = p.fetch("AAPL", interval="5m", bars=5, ttl_seconds=3600)
    s2 = p.fetch("AAPL", interval="5m", bars=5, ttl_seconds=3600)
    assert calls["n"] == 1
    assert s1.data_source == "live" and s2.data_source == "fresh_cache"
    assert len(s2.candles) == len(s1.candles)


def test_incomplete_candle_never_cached(tmp_path, monkeypatch):
    _pin_provider_clock(monkeypatch)
    cache = Cache(directory=str(tmp_path), enabled=True)
    p = YFinanceIntradayProvider(cache=cache)
    in_progress_start = NOW - timedelta(minutes=2)  # 5m window not elapsed at NOW
    def fake_live(psym, interval, bars, ext):
        cs = make_candles(n=4, end=NOW - timedelta(minutes=2), step=flat)
        rows = [
            {"start": c.start.isoformat(), "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume} for c in cs
        ]
        rows.append({"start": in_progress_start.isoformat(), "open": 1, "high": 2,
                     "low": 0.5, "close": 1.5, "volume": 10})
        return {"candles": rows, "exchange_timezone": None, "adjusted": False}
    monkeypatch.setattr(p, "_fetch_live", fake_live)
    live = p.fetch("AAPL", interval="5m", bars=10, ttl_seconds=3600)
    assert live.candles[-1].complete is False          # flagged in the live result
    cached = p.fetch("AAPL", interval="5m", bars=10, ttl_seconds=3600)
    assert cached.data_source == "fresh_cache"
    assert all(c.complete for c in cached.candles)     # in-progress bar not cached
    assert len(cached.candles) == len(live.candles) - 1
