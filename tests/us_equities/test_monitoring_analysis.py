"""Valuation (incl. MXN/USD), trend, earnings events and the selection view."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from asset_selection.monitoring.config import EventsConfig, TrendConfig
from asset_selection.monitoring.market_data import (
    FX_SYMBOL,
    EarningsInfo,
    OverlayMarketData,
    PriceHistory,
    StaticMarketData,
    _normalize_frame,
)
from asset_selection.monitoring.signals import compute_events, compute_trend, load_selection_signals
from asset_selection.monitoring.valuation import assign_weights, compute_valuation, conversion
from asset_selection.monitoring.models import PositionReport

from .monitoring_fixtures import TODAY, constant_frame, holding, price_frame, trend_frame


def _ph(frame, symbol="X"):
    return PriceHistory(symbol, _normalize_frame(frame), source="static")


def _fx_frame(before: float, after: float, switch: date, n: int = 900) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=n)
    vals = [before if d.date() < switch else after for d in idx]
    return pd.DataFrame({"close": vals, "adj_close": vals}, index=idx)


# ---------------------------------------------------------------------------
# Currency conversion
# ---------------------------------------------------------------------------

def test_conversion_factors():
    assert conversion("USD", "USD", None) == 1.0
    assert conversion("USD", "MXN", 18.0) == 18.0
    assert conversion("MXN", "USD", 20.0) == pytest.approx(0.05)
    assert conversion("USD", "MXN", None) is None


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def test_usd_position_reported_in_mxn():
    h = holding(shares=10, unit_cost=100.0, currency="USD", market="US")
    fx = _ph(constant_frame(18.0, n=900), FX_SYMBOL)
    v = compute_valuation(h, _ph(constant_frame(150.0, n=900)), fx, "MXN", today=TODAY)
    assert v.market_value == pytest.approx(1500.0)            # position currency (USD)
    assert v.unrealized_pl_pct == pytest.approx(50.0)
    assert v.market_value_reporting == pytest.approx(27_000.0)  # MXN
    assert v.unrealized_pl_reporting == pytest.approx(9_000.0)


def test_sic_position_splits_return_into_stock_and_fx_exactly():
    """Bought at USD 100 when USDMXN was 20 (MXN 2,000/share); now USD 120 at 18.

    Stock +20% in dollars, peso strengthened 10%: MXN return = 1.2 * 0.9 - 1 = +8%.
    """
    bought = date(2025, 3, 14)
    h = holding(shares=5, unit_cost=2000.0, currency="MXN", market="SIC", purchase_date=bought)
    fx = _ph(_fx_frame(20.0, 18.0, date(2025, 6, 1)), FX_SYMBOL)
    v = compute_valuation(h, _ph(constant_frame(120.0, n=900)), fx, "MXN", today=TODAY)
    assert v.market_value == pytest.approx(5 * 120 * 18)
    assert v.unrealized_pl_pct == pytest.approx(8.0)
    assert v.stock_return_pct == pytest.approx(20.0)
    assert v.fx_return_pct == pytest.approx(-10.0)


def test_multi_lot_split_converts_each_lot_at_its_own_rate():
    from asset_selection.monitoring.holdings import aggregate_lots
    from asset_selection.monitoring.models import Lot

    lots = [
        Lot("NVDA", 1, 2000.0, "MXN", "SIC", date(2025, 3, 14), source="a"),   # USD 100 at 20
        Lot("NVDA", 1, 1800.0, "MXN", "SIC", date(2025, 9, 1), source="b"),    # USD 100 at 18
    ]
    (h,) = aggregate_lots(lots)
    fx = _ph(_fx_frame(20.0, 18.0, date(2025, 6, 1)), FX_SYMBOL)
    v = compute_valuation(h, _ph(constant_frame(100.0, n=900)), fx, "MXN", today=TODAY)
    assert v.stock_return_pct == pytest.approx(0.0)            # USD 200 cost, USD 200 now


def test_split_needs_every_lot_dated():
    h = holding(shares=5, unit_cost=2000.0, currency="MXN", market="SIC", purchase_date=None)
    fx = _ph(constant_frame(18.0, n=900), FX_SYMBOL)
    v = compute_valuation(h, _ph(constant_frame(120.0, n=900)), fx, "MXN", today=TODAY)
    assert v.market_value is not None and v.stock_return_pct is None and v.holding_days is None


def test_sic_without_fx_cannot_be_valued():
    h = holding(currency="MXN", market="SIC")
    v = compute_valuation(h, _ph(constant_frame(120.0)), None, "MXN", today=TODAY)
    assert v.last_price == 120.0 and v.market_value is None


def test_no_prices_means_no_valuation():
    v = compute_valuation(holding(), PriceHistory("X"), None, "USD", today=TODAY)
    assert v.last_price is None and v.market_value is None


def test_drawdown_and_peak_since_purchase():
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=300)
    vals = [100.0] * 100 + [150.0] * 100 + [120.0] * 100
    frame = pd.DataFrame({"close": vals, "adj_close": vals}, index=idx)
    h = holding(purchase_date=idx[50].date())
    v = compute_valuation(h, _ph(frame), None, "USD", today=TODAY)
    assert v.max_gain_since_purchase_pct == pytest.approx(50.0)
    assert v.drawdown_from_peak_since_purchase_pct == pytest.approx(-20.0)


def test_weights_sum_to_one():
    reports = []
    for mv in (100.0, 300.0):
        r = PositionReport(holding=holding())
        r.valuation.market_value_reporting = mv
        reports.append(r)
    assert assign_weights(reports) == 400.0
    assert [r.valuation.weight for r in reports] == [0.25, 0.75]


def test_price_age_and_stale_quote():
    frame = constant_frame(50.0, n=400, end=date(2026, 9, 1))
    v = compute_valuation(holding(), _ph(frame), None, "USD", today=TODAY)
    assert v.price_age_days == (TODAY - date(2026, 9, 1)).days


# ---------------------------------------------------------------------------
# Market data adapters
# ---------------------------------------------------------------------------

def test_normalize_frame_handles_yahoo_columns_and_timezones():
    idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="America/New_York")
    raw = pd.DataFrame({"Close": [1.0, None, 3.0], "Adj Close": [0.9, 2.0, 2.9]}, index=idx)
    out = _normalize_frame(raw)
    assert list(out.columns) == ["close", "adj_close"]
    assert out.index.tz is None and len(out) == 2           # NaN close dropped


def test_static_market_data_reports_missing_symbols():
    md = StaticMarketData({"AAA": constant_frame(10.0)})
    assert md.history("AAA", date(2020, 1, 1)).ok
    miss = md.history("ZZZ", date(2020, 1, 1))
    assert not miss.ok and miss.error_type == "NO_PRICE_DATA"


def test_overlay_pins_todays_price_but_keeps_history():
    base = StaticMarketData({"AAA": constant_frame(10.0, n=50, end=date(2026, 10, 9))})
    h = OverlayMarketData(base, {"aaa": 12.5}).history("AAA", date(2020, 1, 1))
    assert h.last_close == 12.5 and len(h.frame) == 51
    assert "user_override" in h.source


def test_close_on_or_before_uses_last_trading_day():
    h = _ph(trend_frame("up", n=30))
    saturday = date(2026, 10, 10)
    assert h.close_on_or_before(saturday) == pytest.approx(float(h.frame["close"].loc[:"2026-10-09"].iloc[-1]))


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, state, cross", [("up", "UP", "golden"), ("down", "DOWN", "death")])
def test_trend_states(kind, state, cross):
    t = compute_trend(_ph(trend_frame(kind)), TrendConfig())
    assert t.trend_state == state and t.sma_cross == cross


def test_short_history_is_insufficient_not_guessed():
    t = compute_trend(_ph(trend_frame("up", n=120)), TrendConfig())
    assert t.trend_state == "INSUFFICIENT" and t.sma_long is None
    assert t.returns_pct["1m"] is not None and t.returns_pct["12m"] is None


def test_momentum_skips_the_latest_month():
    frame = trend_frame("up", n=400)
    s = frame["adj_close"]
    t = compute_trend(_ph(frame), TrendConfig())
    assert t.momentum_12_1_pct == pytest.approx(100 * (s.iloc[-22] / s.iloc[-253] - 1))


def test_volatility_regime_flags_a_spike():
    calm = price_frame(400, sigma=0.01, seed=3)
    wild = price_frame(70, start=float(calm["close"].iloc[-1]), sigma=0.04, seed=4)
    wild.index = pd.bdate_range(start=calm.index[-1] + pd.Timedelta(days=1), periods=70)
    t = compute_trend(_ph(pd.concat([calm, wild])), TrendConfig())
    assert t.volatility_regime_ratio is not None and t.volatility_regime_ratio > 1.5


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def test_earnings_blackout_and_surprises():
    info = EarningsInfo(next_date=date(2026, 10, 17), history=[
        (date(2026, 7, 30), 1.0, 1.1, 10.0),
        (date(2026, 4, 30), 1.0, 0.9, -10.0),
        (date(2026, 1, 30), 1.0, 1.2, 20.0),
    ])
    e = compute_events(info, EventsConfig(earnings_blackout_days=7), today=TODAY)
    assert e.days_to_earnings == 4 and e.in_earnings_blackout
    assert e.last_surprise_pct == 10.0 and e.beats == 2 and e.reports_counted == 3


def test_no_blackout_when_earnings_are_far():
    e = compute_events(EarningsInfo(next_date=date(2026, 12, 1)), EventsConfig(), today=TODAY)
    assert not e.in_earnings_blackout and e.last_surprise_pct is None


# ---------------------------------------------------------------------------
# Selection view
# ---------------------------------------------------------------------------

def test_load_selection_signals(tmp_path):
    p = tmp_path / "results.csv"
    pd.DataFrame([{
        "rank": 3, "ticker": "nvda", "company_name": "NVIDIA", "sector": "Technology",
        "fundamentals_score": 62.4, "final_score": 70.1, "effective_sentiment_score": 58.0,
        "effective_sentiment_confidence": 0.6, "article_count": 12, "risk_penalty": 1.5,
        "candidate_role": "core_candidate", "eligible_for_allocation": "True",
        "flags": "['LOW_NEWS']",
    }]).to_csv(p, index=False)
    sig = load_selection_signals(p, run_id="r1")["NVDA"]
    assert sig.in_ranking and sig.selection_rank == 3 and sig.eligible_for_allocation is True
    assert sig.effective_sentiment_score == 58.0 and sig.sentiment_confidence == 0.6
    assert sig.flags == ["LOW_NEWS"] and sig.run_id == "r1"


def test_missing_selection_file_is_empty_not_an_error(tmp_path):
    assert load_selection_signals(tmp_path / "nope.csv") == {}
    assert load_selection_signals(None) == {}
