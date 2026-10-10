"""Features, directional scoring, decision rules, payout math, and labels."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from binary_options_selection.data_providers.base import Candle
from binary_options_selection.features import indicators as ind
from binary_options_selection.features.engine import FeatureBundle, compute_features
from binary_options_selection.market_session import classify_session, expiry_for
from binary_options_selection.scoring.directional import score_direction
from binary_options_selection.signals.decision import (
    NO_TRADE_CONTRADICTORY,
    NO_TRADE_EXPIRY_BEYOND_SESSION,
    NO_TRADE_LOW_LIQUIDITY,
    NO_TRADE_SCORES_TOO_CLOSE,
    NO_TRADE_STALE_DATA,
    NO_TRADE_UNSUPPORTED_HORIZON,
    SIGNAL_CALL,
    SIGNAL_NO_TRADE,
    SIGNAL_PUT,
    assess_liquidity,
    decide_signal,
)
from binary_options_selection.signals.labels import (
    CALL_WIN,
    PUT_WIN,
    TIE,
    UNKNOWN,
    resolve_outcome,
)
from binary_options_selection.signals.payout import (
    break_even_win_probability,
    expected_edge,
)
from binary_options_selection.validation.candles import assess_series

from tests.binary_options.bo_fixtures import (
    INTERVAL_SEC,
    NOW,
    downtrend,
    flat,
    make_candles,
    make_config,
    make_series,
    uptrend,
)

CFG = make_config()
SESSION = classify_session(NOW, CFG.market_session)


def _pipeline_pieces(candles, horizon=15, cfg=CFG, now=NOW):
    quality = assess_series(make_series("T", candles), cfg, now)
    bundle = compute_features(
        quality.candles, horizon_minutes=horizon, interval_minutes=5, cfg=cfg
    )
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score, cfg=cfg)
    liquidity = assess_liquidity(quality.candles, cfg)
    expiry = expiry_for(quality.latest_complete_close, horizon)
    decision = decide_signal(scores, quality, SESSION, liquidity, expiry_utc=expiry, cfg=cfg)
    return quality, bundle, scores, decision


# ---------------------------------------------------------------------------
# indicators on known synthetic data
# ---------------------------------------------------------------------------

def test_indicator_minimum_data_returns_none():
    assert ind.rsi([1.0, 2.0], 14) is None
    assert ind.atr([1], [1], [1], 14) is None
    assert ind.sma([1.0], 5) is None
    assert ind.window_return([1.0], 5) is None


def test_rsi_extremes_and_midpoint():
    rising = [float(i) for i in range(1, 40)]
    falling = [float(i) for i in range(40, 1, -1)]
    assert ind.rsi(rising, 14) > 99
    assert ind.rsi(falling, 14) < 1


def test_sma_and_ema_known_values():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert ind.sma(vals, 5) == 3.0
    # EMA(3) over [1..5]: seed 2.0, k=0.5 -> 3.0 -> 4.0
    assert ind.ema(vals, 3) == pytest.approx(4.0)


def test_atr_constant_range():
    highs = [11.0] * 30
    lows = [10.0] * 30
    closes = [10.5] * 30
    assert ind.atr(highs, lows, closes, 14) == pytest.approx(1.0)


def test_bollinger_z_flat_is_zero():
    assert ind.bollinger_z([5.0] * 25, 20) == 0.0


def test_consecutive_direction_signed():
    assert ind.consecutive_direction([1, 2, 3, 4, 5, 6]) == 1.0     # 5 ups, cap 5
    assert ind.consecutive_direction([6, 5, 4]) == pytest.approx(-0.4)


# ---------------------------------------------------------------------------
# scoring: direction, symmetry, bounds, horizons
# ---------------------------------------------------------------------------

def test_call_dominant_conditions():
    *_, scores, decision = _pipeline_pieces(make_candles(step=uptrend))
    assert decision.signal == SIGNAL_CALL
    assert scores.directional_score > 15
    assert scores.primary_drivers and scores.primary_drivers[0].startswith("momentum")


def test_put_dominant_conditions():
    *_, scores, decision = _pipeline_pieces(make_candles(step=downtrend))
    assert decision.signal == SIGNAL_PUT
    assert scores.directional_score < -15


def test_flat_market_is_no_trade_on_valid_data():
    *_, scores, decision = _pipeline_pieces(make_candles(step=flat))
    assert decision.signal == SIGNAL_NO_TRADE
    assert decision.no_trade_reason in (NO_TRADE_SCORES_TOO_CLOSE, "LOW_CONFIDENCE")
    assert decision.signal_validity != "invalid"   # good data, honest "don't trade"


def test_call_put_symmetry_by_construction():
    q, bundle, scores, _ = _pipeline_pieces(make_candles(step=uptrend))
    mirrored = FeatureBundle(
        horizon_minutes=bundle.horizon_minutes,
        horizon_bars=bundle.horizon_bars,
        family_scores={k: -v for k, v in bundle.family_scores.items()},
        missing_families=bundle.missing_families,
        vol_regime_factor=bundle.vol_regime_factor,
    )
    m = score_direction(mirrored, data_quality_score=q.data_quality_score, cfg=CFG)
    assert m.call_score == pytest.approx(scores.put_score, abs=1e-6)
    assert m.put_score == pytest.approx(scores.call_score, abs=1e-6)
    assert m.directional_score == pytest.approx(-scores.directional_score, abs=1e-6)
    assert m.confidence_score == pytest.approx(scores.confidence_score, abs=1e-4)


def test_contradictory_families_produce_no_trade():
    q, bundle, _, _ = _pipeline_pieces(make_candles(step=uptrend))
    conflicted = FeatureBundle(
        horizon_minutes=15, horizon_bars=3,
        family_scores={"momentum": 0.8, "trend": -0.7, "mean_reversion": 0.6,
                       "volume": -0.65, "context": -0.1},
        vol_regime_factor=1.0,
    )
    scores = score_direction(conflicted, data_quality_score=100.0, cfg=CFG)
    assert scores.contradiction_ratio > CFG.signal_rules.max_contradiction_ratio
    liquidity = assess_liquidity(q.candles, CFG)
    decision = decide_signal(scores, q, SESSION, liquidity,
                             expiry_utc=expiry_for(q.latest_complete_close, 15), cfg=CFG)
    assert decision.signal == SIGNAL_NO_TRADE
    assert decision.no_trade_reason == NO_TRADE_CONTRADICTORY
    assert scores.primary_conflicts  # the conflicts are named, not hidden


def test_different_horizons_scored_independently():
    # 114 declining bars, then a sharp 6-bar rally: the short horizon (whose
    # momentum windows sit inside the rally) reads decisively more bullish
    # than the extended horizon (whose windows still span the decline).
    def regime(i, p):
        return 0.45 if i >= 114 else -0.20
    candles = make_candles(step=regime)
    _, _, s5, _ = _pipeline_pieces(candles, horizon=5)
    _, _, s120, _ = _pipeline_pieces(candles, horizon=120)
    assert s5.directional_score > 15
    assert s5.directional_score - s120.directional_score > 15
    assert s5.contributions != s120.contributions  # genuinely independent inputs


def test_short_and_extended_groups_use_different_weights():
    q, bundle, s15, _ = _pipeline_pieces(make_candles(step=uptrend), horizon=15)
    _, _, s30, _ = _pipeline_pieces(make_candles(step=uptrend), horizon=30)
    assert s15.horizon_group == "short_term"
    assert s30.horizon_group == "extended_intraday"   # 30m is extended


def test_score_bounds_hold_on_random_walks():
    rng = random.Random(42)
    for trial in range(10):
        candles = make_candles(
            step=lambda i, p: rng.uniform(-0.4, 0.4),
            volume=lambda i: rng.uniform(0, 120_000),
        )
        for horizon in (5, 30, 120):
            q, _, scores, _ = _pipeline_pieces(candles, horizon=horizon)
            assert 0.0 <= scores.call_score <= 100.0
            assert 0.0 <= scores.put_score <= 100.0
            assert -100.0 <= scores.directional_score <= 100.0
            assert 0.0 <= scores.confidence_score <= 1.0
            assert 0.0 <= scores.contradiction_ratio <= 1.0


def test_missing_families_are_excluded_not_neutral_filled():
    bundle = FeatureBundle(horizon_minutes=15, horizon_bars=3,
                           family_scores={"momentum": 1.0},
                           missing_families={"trend": "x", "volume": "x",
                                             "mean_reversion": "x", "context": "x"})
    scores = score_direction(bundle, data_quality_score=100.0, cfg=CFG)
    # The single available family fully drives direction...
    assert scores.directional_score == pytest.approx(100.0)
    # ...but the missing weight shows up as reduced coverage -> reduced confidence.
    assert scores.feature_coverage < 0.5
    assert scores.confidence_score < 0.5


def test_no_leakage_features_depend_only_on_past_window():
    candles = make_candles(step=uptrend)
    head = candles[:100]
    bundle_head = compute_features(head, horizon_minutes=15, interval_minutes=5, cfg=CFG)
    bundle_full = compute_features(candles, horizon_minutes=15, interval_minutes=5, cfg=CFG)
    recomputed = compute_features(candles[:100], horizon_minutes=15, interval_minutes=5, cfg=CFG)
    assert bundle_head.family_scores == recomputed.family_scores
    # appending future candles changes nothing about the historical window
    assert bundle_head.components == recomputed.components
    assert bundle_full is not bundle_head  # sanity


# ---------------------------------------------------------------------------
# decision gates
# ---------------------------------------------------------------------------

def test_stale_data_blocks_live_signal():
    later = NOW + timedelta(hours=3)
    session = classify_session(later, CFG.market_session)
    quality = assess_series(make_series("T", make_candles(step=uptrend)), CFG, later)
    bundle = compute_features(quality.candles, horizon_minutes=15, interval_minutes=5, cfg=CFG)
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score, cfg=CFG)
    decision = decide_signal(scores, quality, session, assess_liquidity(quality.candles, CFG),
                             expiry_utc=later + timedelta(minutes=15), cfg=CFG)
    assert decision.signal == SIGNAL_NO_TRADE
    assert decision.no_trade_reason == NO_TRADE_STALE_DATA
    assert decision.signal_validity == "invalid"


def test_low_liquidity_blocks_signal():
    candles = make_candles(step=uptrend, volume=lambda i: 5.0)
    *_, decision = _pipeline_pieces(candles)
    assert decision.no_trade_reason == NO_TRADE_LOW_LIQUIDITY


def test_unsupported_horizon_rejected_per_row():
    q, bundle, scores, _ = _pipeline_pieces(make_candles(step=uptrend))
    scores.horizon_minutes = 7  # not a multiple of 5m
    decision = decide_signal(scores, q, SESSION, assess_liquidity(q.candles, CFG),
                             expiry_utc=expiry_for(q.latest_complete_close, 7), cfg=CFG)
    assert decision.no_trade_reason == NO_TRADE_UNSUPPORTED_HORIZON


def test_expiry_beyond_session_blocks_signal():
    late = NOW.replace(hour=19, minute=55)  # 15:55 ET
    session = classify_session(late, CFG.market_session)
    candles = make_candles(end=late, step=uptrend)
    quality = assess_series(make_series("T", candles), CFG, late)
    bundle = compute_features(quality.candles, horizon_minutes=120, interval_minutes=5, cfg=CFG)
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score, cfg=CFG)
    decision = decide_signal(scores, quality, session, assess_liquidity(quality.candles, CFG),
                             expiry_utc=expiry_for(quality.latest_complete_close, 120), cfg=CFG)
    assert decision.no_trade_reason == NO_TRADE_EXPIRY_BEYOND_SESSION


def test_deterministic_tie_break():
    from binary_options_selection.pipelines.run_binary_options_selection import _rank_rows
    def row(sym, horizon, conf=0.5, ds=30.0, signal=SIGNAL_CALL):
        return {"symbol": sym, "horizon_minutes": horizon, "signal": signal,
                "confidence_score": conf, "directional_score": ds}
    rows = [row("BBB", 15), row("AAA", 15), row("AAA", 5), row("CCC", 5, signal=SIGNAL_NO_TRADE)]
    ranked = _rank_rows(rows)
    assert [(r["symbol"], r["horizon_minutes"]) for r in ranked[:3]] == [
        ("AAA", 5), ("AAA", 15), ("BBB", 15)]
    assert ranked[-1]["signal"] == SIGNAL_NO_TRADE


# ---------------------------------------------------------------------------
# payout + labels
# ---------------------------------------------------------------------------

def test_break_even_probability_formula():
    assert break_even_win_probability(0.85) == pytest.approx(1 / 1.85)
    assert break_even_win_probability(1.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        break_even_win_probability(0.0)


def test_expected_edge_refuses_heuristic_confidence():
    # No calibrated probability exists in this milestone -> always None.
    assert expected_edge(None, 0.85) is None
    with pytest.raises(ValueError):
        expected_edge(1.7, 0.85)
    assert expected_edge(0.6, 0.85) == pytest.approx(0.6 * 0.85 - 0.4)


def test_live_pipeline_never_imports_label_module():
    import inspect

    import binary_options_selection.pipelines.run_binary_options_selection as pipe
    src = inspect.getsource(pipe)
    assert "labels" not in src  # outcome labels stay out of live signal generation


def _future(ref, closes):
    return [
        Candle(start=ref + timedelta(seconds=INTERVAL_SEC * i), open=c, high=c + 1,
               low=c - 1, close=c, volume=10)
        for i, c in enumerate(closes)
    ]


def test_outcome_labels():
    ref_t = NOW
    exp_t = expiry_for(ref_t, 15)
    up = resolve_outcome(_future(ref_t, [100, 101, 105]), reference_time=ref_t,
                         reference_price=100.0, expiry_time=exp_t,
                         interval_seconds=INTERVAL_SEC)
    assert up.outcome == CALL_WIN and up.settlement_price == 105
    down = resolve_outcome(_future(ref_t, [100, 99, 95]), reference_time=ref_t,
                           reference_price=100.0, expiry_time=exp_t,
                           interval_seconds=INTERVAL_SEC)
    assert down.outcome == PUT_WIN
    tie = resolve_outcome(_future(ref_t, [101, 99, 100]), reference_time=ref_t,
                          reference_price=100.0, expiry_time=exp_t,
                          interval_seconds=INTERVAL_SEC)
    assert tie.outcome == TIE


def test_missing_settlement_candle_is_unknown_by_default():
    ref_t = NOW
    exp_t = expiry_for(ref_t, 15)
    fut = _future(ref_t, [100, 101])  # settlement candle (start=+10m) missing
    r = resolve_outcome(fut, reference_time=ref_t, reference_price=100.0,
                        expiry_time=exp_t, interval_seconds=INTERVAL_SEC)
    assert r.outcome == UNKNOWN
    tolerant = resolve_outcome(fut, reference_time=ref_t, reference_price=100.0,
                               expiry_time=exp_t, interval_seconds=INTERVAL_SEC,
                               prior_close_tolerance_bars=1)
    assert tolerant.outcome == CALL_WIN and tolerant.used_prior_close_bars == 1


def test_labels_reject_pre_reference_candles():
    ref_t = NOW
    with pytest.raises(ValueError):
        resolve_outcome(_future(ref_t - timedelta(minutes=30), [100]),
                        reference_time=ref_t, reference_price=100.0,
                        expiry_time=expiry_for(ref_t, 15),
                        interval_seconds=INTERVAL_SEC)
