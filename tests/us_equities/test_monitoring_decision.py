"""Decision rules: SELL / TRIM / HOLD / ADD / REVIEW, sizing and funding."""
from __future__ import annotations

from typing import List, Optional

import pytest

from asset_selection.monitoring.config import MonitoringConfig
from asset_selection.monitoring.decision import decide_all, effective_cap, thesis_status
from asset_selection.monitoring.models import (
    ADD,
    HOLD,
    REVIEW,
    SELL,
    THESIS_BROKEN,
    THESIS_INTACT,
    THESIS_STRATEGIC,
    THESIS_UNKNOWN,
    THESIS_WEAKENED,
    TRIM,
    EventSignals,
    ForecastResult,
    HorizonForecast,
    PositionReport,
    SelectionSignals,
    SkillReport,
    TrendSignals,
)

from .monitoring_fixtures import TODAY, holding


def _cfg(**decision) -> MonitoringConfig:
    cfg = MonitoringConfig()
    cfg.run.reporting_currency = "USD"
    cfg.decision.min_trade_value = 100.0
    for k, v in decision.items():
        setattr(cfg.decision, k, v)
    return cfg


def _selection(fund=60.0, eligible=True, role="core_candidate", sentiment=55.0, conf=0.6):
    return SelectionSignals(source="full_ranking", in_ranking=True, fundamentals_score=fund,
                            eligible_for_allocation=eligible, candidate_role=role,
                            effective_sentiment_score=sentiment, sentiment_confidence=conf)


def _trend(state="MIXED", momentum: Optional[float] = None):
    return TrendSignals(history_days=400, trend_state=state, price_vs_sma_long_pct=5.0,
                        sma_cross="golden" if state == "UP" else "death", momentum_12_1_pct=momentum)


def _report(ticker="AAA", *, shares=10.0, cost=100.0, price=100.0, selection=None, trend=None,
            events=None, forecast=None, drawdown=None) -> PositionReport:
    r = PositionReport(holding=holding(ticker, shares=shares, unit_cost=cost))
    r.valuation.last_price = price
    r.valuation.fx_rate = None
    r.valuation.market_value = shares * price
    r.valuation.market_value_reporting = shares * price
    r.valuation.cost_basis_reporting = shares * cost
    r.valuation.unrealized_pl_pct = 100 * (price / cost - 1)
    r.valuation.price_age_days = 1
    r.valuation.drawdown_from_peak_since_purchase_pct = drawdown
    r.selection = selection if selection is not None else _selection()
    r.trend = trend if trend is not None else _trend()
    r.events = events if events is not None else EventSignals()
    r.forecast = forecast
    return r


def _run(reports: List[PositionReport], cfg=None, **kw) -> float:
    cfg = cfg or _cfg()
    total = sum(r.valuation.market_value_reporting or 0 for r in reports)
    for r in reports:
        if r.valuation.market_value_reporting is not None:
            r.valuation.weight = r.valuation.market_value_reporting / total
    return decide_all(reports, cfg, total_value=total, today=TODAY, **kw)


def _eight(**kw) -> List[PositionReport]:
    """An equal-weight 8-name book (12.5% each, under the 15% cap)."""
    return [_report(f"T{i}", **kw) for i in range(8)]


# ---------------------------------------------------------------------------
# Thesis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sel, status", [
    (_selection(fund=62), THESIS_INTACT),
    (_selection(fund=45), THESIS_WEAKENED),
    (_selection(fund=48, eligible=False), THESIS_WEAKENED),
    (_selection(fund=35), THESIS_BROKEN),
    (SelectionSignals(source="full_ranking", in_ranking=False), THESIS_UNKNOWN),
    (SelectionSignals(), THESIS_UNKNOWN),
])
def test_thesis_status(sel, status):
    r = _report(selection=sel)
    assert thesis_status(r, _cfg().decision)[0] == status


def test_thesis_is_about_the_business_not_the_price():
    """Price-driven exclusions are trend/risk evidence, never a broken thesis."""
    sel = _selection(fund=60, eligible=False, role="watchlist_only")
    sel.exclusion_reasons = ["watchlist_only", "negative_recent_return", "high_volatility"]
    r = _report(selection=sel)
    assert thesis_status(r, _cfg().decision)[0] == THESIS_INTACT
    book = [r] + _eight()[1:]
    _run(book)
    risk = [e for e in r.recommendation.evidence if e.factor == "risk"]
    assert risk and risk[0].score == -0.5 and "high_volatility" in risk[0].summary


def test_business_exclusions_weaken_the_thesis():
    sel = _selection(fund=60, eligible=False)
    sel.exclusion_reasons = ["missing_market_cap"]
    assert thesis_status(_report(selection=sel), _cfg().decision)[0] == THESIS_WEAKENED


def test_incomplete_data_is_unknown_never_broken():
    sel = _selection(fund=30)
    sel.data_quality_bucket, sel.missing_metric_count = "incomplete", 14
    status, why = thesis_status(_report(selection=sel), _cfg().decision)
    assert status == THESIS_UNKNOWN and "incomplete" in why


def test_mexican_listing_thesis_is_unknown():
    r = _report()
    r.holding.in_selection_coverage = False
    assert thesis_status(r, _cfg().decision)[0] == THESIS_UNKNOWN


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def test_unvalued_position_needs_review():
    r = _report()
    r.valuation.market_value_reporting = None
    others = _eight()
    _run([r] + others)
    assert r.recommendation.action == REVIEW


def test_broken_thesis_sells_everything():
    book = _eight()
    book[0].selection = _selection(fund=30)
    _run(book)
    rec = book[0].recommendation
    assert rec.action == SELL and rec.shares_delta == -10
    assert rec.value_delta_reporting == pytest.approx(-1000.0)
    assert rec.commission_reporting == pytest.approx(2.0)
    assert "thesis broken" in rec.rule


def test_a_loss_alone_never_sells():
    book = _eight()
    book[0] = _report("T0", cost=200.0, price=100.0, drawdown=-50.0)     # down 50%, thesis intact
    _run(book)
    rec = book[0].recommendation
    assert rec.action == HOLD
    assert any(b.startswith("DRAWDOWN_REVIEW") for b in rec.blockers)


def test_a_gain_alone_never_sells():
    book = _eight(cost=20.0, price=100.0)                                 # every name up 400%
    _run(book)
    assert all(r.recommendation.action == HOLD for r in book)


def test_overweight_versus_target_trims_back_to_target():
    big = _report("BIG", shares=60)            # 6000 of 7000 = 85.7%
    small = _report("SMALL", shares=10)
    _run([big, small], targets={"BIG": 0.5, "SMALL": 0.5})
    rec = big.recommendation
    assert rec.action == TRIM
    assert rec.shares_delta == -25           # ceil((6000 - 3500) / 100)
    assert "overweight" in rec.rule


def test_within_band_is_not_trimmed():
    a, b = _report("A", shares=11), _report("B", shares=9)   # 55% vs target 50%: inside +25% band
    _run([a, b], targets={"A": 0.5, "B": 0.5})
    assert a.recommendation.action == HOLD


def test_concentration_cap_without_target():
    book = _eight()
    book[0] = _report("T0", shares=40)          # 4000 of 11000 = 36%
    _run(book)
    rec = book[0].recommendation
    assert rec.action == TRIM and "concentration" in rec.rule


def test_cap_widens_when_it_is_mathematically_unreachable():
    assert effective_cap(0.15, 0.25, 3) == pytest.approx(1.25 / 3)
    assert effective_cap(0.15, 0.25, 20) == 0.15
    trio = [_report("A", shares=10), _report("B", shares=10), _report("C", shares=10)]
    _run(trio)                                  # 33% each: fine for a 3-name book
    assert all(r.recommendation.action != TRIM for r in trio)


def test_weakened_thesis_with_negative_evidence_trims_half():
    book = _eight()
    book[0].selection = _selection(fund=45)
    book[0].trend = _trend("DOWN", momentum=-20.0)
    _run(book)
    rec = book[0].recommendation
    assert rec.action == TRIM and rec.shares_delta == -5
    assert "weakened" in rec.rule


def test_weakened_thesis_without_negative_evidence_holds():
    book = _eight()
    book[0].selection = _selection(fund=45)
    book[0].trend = _trend("UP", momentum=10.0)
    _run(book)
    assert book[0].recommendation.action == HOLD


def _strong(**kw) -> PositionReport:
    return _report(selection=_selection(fund=65, sentiment=70), trend=_trend("UP", momentum=25.0), **kw)


def test_strong_evidence_adds_when_funded():
    book = _eight()
    book[0] = _strong(shares=5)                 # 500 of 7500 = 6.7%, room under 15%
    left = _run(book, new_cash=1000.0)
    rec = book[0].recommendation
    assert rec.action == ADD and rec.shares_delta > 0
    assert rec.value_delta_reporting + rec.commission_reporting <= 1000.0 + 1e-9
    assert left == pytest.approx(1000.0 - rec.value_delta_reporting - rec.commission_reporting)


def test_add_without_money_is_flagged_unfunded():
    book = _eight()
    book[0] = _strong(shares=5)
    _run(book, new_cash=0.0)
    rec = book[0].recommendation
    assert rec.action == ADD and rec.shares_delta == 0
    assert any(b.startswith("UNFUNDED") for b in rec.blockers)


def test_earnings_blackout_defers_an_add():
    book = _eight()
    book[0] = _strong(shares=5, events=EventSignals(next_earnings_date="2026-10-16",
                                                    days_to_earnings=3, in_earnings_blackout=True))
    _run(book, new_cash=1000.0)
    rec = book[0].recommendation
    assert rec.action == HOLD
    assert any(b.startswith("EARNINGS_BLACKOUT") for b in rec.blockers)


def test_sale_proceeds_fund_adds_without_double_counting():
    """A rebalance: sell a broken name and put the money into a strong one."""
    book = _eight()
    book[0].selection = _selection(fund=30)                      # SELL 10 x 100
    book[1] = _strong(shares=5)                                  # ADD candidate
    left = _run(book, new_cash=0.0)
    sell, add = book[0].recommendation, book[1].recommendation
    assert sell.action == SELL and add.action == ADD and add.shares_delta > 0
    total = sum(r.valuation.market_value_reporting for r in book)
    # Target value is a share of the CURRENT portfolio (proceeds were already inside it).
    assert 500 + add.value_delta_reporting <= effective_cap(0.15, 0.25, 8) * total + 100
    spent = add.value_delta_reporting + add.commission_reporting
    assert left == pytest.approx(1000.0 - 2.0 - spent)


def test_proceeds_kept_as_cash_when_reinvest_is_off():
    book = _eight()
    book[0].selection = _selection(fund=30)
    book[1] = _strong(shares=5)
    left = _run(book, cfg=_cfg(reinvest_proceeds=False), new_cash=0.0)
    assert book[1].recommendation.shares_delta == 0
    assert left == pytest.approx(998.0)


def test_fractional_shares_when_enabled():
    book = _eight()
    book[0] = _strong(shares=0.5, price=1000.0)          # 500 of 7500: room under the cap
    _run(book, cfg=_cfg(allow_fractional_shares=True), new_cash=1500.0)
    rec = book[0].recommendation
    assert rec.action == ADD and rec.shares_delta != int(rec.shares_delta)


# ---------------------------------------------------------------------------
# Forecast evidence is gated
# ---------------------------------------------------------------------------

def _forecast(directional: bool, informative: bool, p_up: float = 0.9) -> ForecastResult:
    fc = ForecastResult(backend="chronos" if directional else "bootstrap", directional=directional,
                        status="ok")
    fc.horizons = [HorizonForecast(63, {0.1: 90.0, 0.5: 110.0, 0.9: 130.0}, p_up=p_up)]
    fc.skill = {63: SkillReport(63, 24, 0.5, 1.0, 0.5, -3.0, 0.001, 0.8, [0.6, 0.9], 0.8,
                                informative=informative, p_value_adjusted=0.003, effective_windows=8)}
    return fc


def _forecast_score(fc) -> float:
    book = _eight()
    book[0].forecast = fc
    _run(book)
    return next(e.score for e in book[0].recommendation.evidence if e.factor == "forecast")


def test_validated_directional_forecast_counts():
    assert _forecast_score(_forecast(True, True, p_up=0.9)) == pytest.approx(0.5)


def test_unvalidated_forecast_does_not_count():
    assert _forecast_score(_forecast(True, False, p_up=0.9)) == 0.0


def test_non_directional_forecast_never_counts_even_if_it_scored_well():
    assert _forecast_score(_forecast(False, True, p_up=0.9)) == 0.0


# ---------------------------------------------------------------------------
# Evidence text
# ---------------------------------------------------------------------------

def test_low_confidence_sentiment_is_ignored():
    book = _eight()
    book[0].selection = _selection(sentiment=90.0, conf=0.1)
    _run(book)
    ev = next(e for e in book[0].recommendation.evidence if e.factor == "sentiment")
    assert ev.score == 0.0 and "low confidence" in ev.summary


def test_review_date_follows_earnings():
    book = _eight()
    book[0].events = EventSignals(next_earnings_date="2026-11-17", days_to_earnings=35)
    _run(book)
    assert book[0].recommendation.review_by == "2026-11-19"


# ---------------------------------------------------------------------------
# Strategic holdings (index / bond funds)
# ---------------------------------------------------------------------------

def _fund(ticker="VOO", sleeve="index_core", **kw) -> PositionReport:
    r = _report(ticker, **kw)
    r.holding.sleeve = sleeve
    return r


@pytest.mark.parametrize("make", [
    lambda: _fund(),
    lambda: _fund("BND", sleeve="bond_proxy"),
    lambda: _report(selection=SelectionSignals(source="full_ranking", in_ranking=True, is_etf=True)),
])
def test_funds_are_strategic(make):
    assert thesis_status(make(), _cfg().decision)[0] == THESIS_STRATEGIC


def test_a_fund_is_never_sold_on_fundamentals():
    """The bug real data exposed: an index ETF 'failing' a company-fundamentals test."""
    voo = _fund(selection=_selection(fund=30))
    book = [voo] + _eight()[1:]
    _run(book)
    assert voo.recommendation.action == HOLD
    assert voo.recommendation.thesis_status == THESIS_STRATEGIC


def test_a_fund_is_exempt_from_the_single_name_cap():
    voo = _fund(shares=60)                        # 6000 of 13000 = 46%
    book = [voo] + _eight()[1:]
    _run(book)
    assert voo.recommendation.action == HOLD


def test_an_underweight_fund_is_rebalanced_up_first():
    voo = _fund(shares=5, trend=_trend("DOWN", momentum=-15.0))   # 33% vs 60% target; trend irrelevant
    stock = _strong(ticker="STK", shares=10)
    targets = {"VOO": 0.6, "STK": 0.4}
    _run([voo, stock], targets=targets, new_cash=300.0)
    assert voo.recommendation.action == ADD and voo.recommendation.shares_delta > 0
    assert "rebalance" in voo.recommendation.rule


def test_an_overweight_fund_is_trimmed_to_target():
    voo = _fund(shares=90)
    stock = _report("STK", shares=10)
    _run([voo, stock], targets={"VOO": 0.5, "STK": 0.5})
    assert voo.recommendation.action == TRIM


# ---------------------------------------------------------------------------
# Refreshing selection signals
# ---------------------------------------------------------------------------

def test_refresh_updates_only_absolute_sentiment():
    from asset_selection.monitoring.signals import merge_refreshed_sentiment

    base = {"AFL": _selection(fund=58.7, sentiment=50.0, conf=0.2)}
    fresh = {"AFL": _selection(fund=45.1, eligible=False, role="watchlist_only", sentiment=66.0, conf=0.7)}
    merged = merge_refreshed_sentiment(base, fresh)["AFL"]
    # Fundamentals are z-scores across the scored set: a 12-stock rescore is not comparable.
    assert merged.fundamentals_score == 58.7 and merged.candidate_role == "core_candidate"
    assert merged.effective_sentiment_score == 66.0 and merged.sentiment_confidence == 0.7
    assert merged.sentiment_source == "refreshed"


def test_refresh_for_a_name_outside_the_ranking_adds_news_but_no_thesis():
    from asset_selection.monitoring.signals import merge_refreshed_sentiment

    merged = merge_refreshed_sentiment({"X": _selection()}, {"NEW": _selection(fund=99, sentiment=70)})
    assert merged["NEW"].in_ranking is False and merged["NEW"].fundamentals_score is None
    assert merged["NEW"].effective_sentiment_score == 70


def test_add_waits_for_the_band_like_trim_does():
    """A strong name a hair under its target is not worth an order (no churn)."""
    a = _strong(ticker="A", shares=48)              # 48% vs 50% target: inside the band
    b = _report("B", shares=52)
    _run([a, b], targets={"A": 0.5, "B": 0.5}, new_cash=1000.0)
    rec = a.recommendation
    assert rec.action == HOLD and "within its target band" in rec.rule
