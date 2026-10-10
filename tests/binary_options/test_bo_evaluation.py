"""Historical evaluation: leakage guards, splits, labels, baselines, metrics."""
from __future__ import annotations

import pytest

from binary_options_selection.evaluation import (
    BASELINES,
    EvalRecord,
    EvaluationConfig,
    compare_to_baselines,
    evaluate_with_baselines,
    full_report,
    replay,
    summarize,
)

from tests.binary_options.bo_fixtures import (
    make_candles,
    make_config,
    trend_volume,
    uptrend,
)

CFG = make_config()
EC = EvaluationConfig(horizons=[5, 15, 30, 60], step_bars=12, payout_ratio=0.85)


def _candles(n=360):
    return make_candles(n=n, step=uptrend, volume=trend_volume)


def test_replay_produces_records_for_every_horizon():
    recs = replay(_candles(), CFG, EC)
    horizons = {r.horizon_minutes for r in recs}
    assert horizons == {5, 15, 30, 60}
    groups = {r.horizon_group for r in recs}
    assert groups == {"short_term", "extended_intraday"}


def test_no_future_leakage():
    candles = _candles()
    base = replay(candles, CFG, EC)
    mutated = _candles()
    cut = mutated[300].start.isoformat()
    for c in mutated[300:]:
        c.close *= 3.0
        c.high *= 3.0
    changed = replay(mutated, CFG, EC)
    for a, b in zip(base, changed):
        if a.timestamp < cut:
            assert a.signal == b.signal and a.confidence == b.confidence


def test_chronological_splits_are_ordered_and_test_untouched():
    recs = replay(_candles(), CFG, EC)
    stamps = {s: r.split for r in recs for s in [r.timestamp]}
    ordered = sorted(stamps)
    labels = [stamps[t] for t in ordered]
    # once we leave train we never return; once in test we stay in test
    assert labels == sorted(labels, key=lambda x: ("train", "validation", "test").index(x))
    assert labels[-1] == "test" and labels[0] == "train"


def test_outcomes_and_correctness_in_uptrend():
    recs = [r for r in replay(_candles(), CFG, EC) if r.signal != "NO_TRADE"]
    resolved = [r for r in recs if r.outcome in ("CALL_WIN", "PUT_WIN")]
    assert resolved, "the steady uptrend must resolve some outcomes"
    for r in resolved:
        assert r.correct is (r.signal == "CALL" and r.outcome == "CALL_WIN") or (
            r.signal == "PUT" and r.outcome == "PUT_WIN"
        )
    # near the end of the series outcomes become UNKNOWN, never losses
    unknown = [r for r in recs if r.outcome == "UNKNOWN"]
    for r in unknown:
        assert r.correct is None


def test_baselines_deterministic_and_complete():
    a = evaluate_with_baselines(_candles(), CFG, EC)
    b = evaluate_with_baselines(_candles(), CFG, EC)
    assert set(a) == {"strategy", *BASELINES}
    for name in a:
        assert [r.signal for r in a[name]] == [r.signal for r in b[name]]
    always = a["always_call"]
    assert all(r.signal == "CALL" for r in always)


def _rec(signal, outcome, *, conf=0.5, ts="2026-01-01T00:00:00", h=15, payout=1.0):
    correct = None
    if signal != "NO_TRADE" and outcome in ("CALL_WIN", "PUT_WIN"):
        correct = (signal == "CALL") == (outcome == "CALL_WIN")
    return EvalRecord(
        timestamp=ts, horizon_minutes=h, horizon_group="short_term",
        signal=signal, confidence=conf, strength="strong",
        outcome=outcome, correct=correct, payout_ratio=payout,
    )


def test_metric_arithmetic_on_constructed_records():
    recs = [
        _rec("CALL", "CALL_WIN"),          # +1.0
        _rec("CALL", "PUT_WIN"),           # -1.0
        _rec("PUT", "PUT_WIN"),            # +1.0
        _rec("PUT", "CALL_WIN"),           # -1.0
        _rec("CALL", "TIE"),               # 0
        _rec("CALL", "UNKNOWN"),           # excluded from returns
        _rec("NO_TRADE", "UNKNOWN"),
    ]
    s = summarize(recs, EC)
    assert s["signal_count"] == 7
    assert s["call_count"] == 4 and s["put_count"] == 2 and s["no_trade_count"] == 1
    assert s["coverage_rate"] == pytest.approx(6 / 7, abs=1e-3)
    assert s["combined_win_rate"] == pytest.approx(0.5)
    assert s["call_win_rate"] == pytest.approx(0.5)
    assert s["put_win_rate"] == pytest.approx(0.5)
    assert s["tie_rate"] == pytest.approx(1 / 5)
    assert s["unknown_rate"] == pytest.approx(1 / 6, abs=1e-3)
    assert s["average_payout"] == pytest.approx(1.0)
    assert s["break_even_win_rate"] == pytest.approx(0.5)
    assert s["realized_return_per_unit_staked"] == pytest.approx(0.0)
    assert s["maximum_consecutive_losses"] == 1
    assert s["maximum_drawdown"] == pytest.approx(1.0)


def test_expected_value_is_never_derived_from_confidence():
    report = full_report(replay(_candles(), CFG, EC), EC)
    blob = str(report)
    assert "expected_value" not in blob and "expected_edge" not in blob
    assert "predicted_probability" not in blob
    assert any("not a calibrated probability" in n for n in report["notes"])


def test_confidence_buckets_reported_not_calibrated():
    report = full_report(replay(_candles(), CFG, EC), EC)
    assert report["performance_by_confidence_bucket"]
    assert "brier" not in str(report).lower()


def test_baseline_comparison_verdicts():
    reports = {
        "strategy": {"test": {"realized_return_per_unit_staked": 0.5,
                              "combined_win_rate": 0.8, "wins": 40, "losses": 10}},
        "always_call": {"test": {"realized_return_per_unit_staked": 0.1,
                                 "combined_win_rate": 0.55}},
    }
    good = compare_to_baselines(reports, min_observations=30)
    assert "outperformed" in good["verdict"]
    reports["always_call"]["test"]["realized_return_per_unit_staked"] = 0.9
    bad = compare_to_baselines(reports, min_observations=30)
    assert "NOT outperform" in bad["verdict"]
    thin = compare_to_baselines(
        {"strategy": {"test": {"realized_return_per_unit_staked": 1.0,
                               "combined_win_rate": 1.0, "wins": 2, "losses": 0}},
         "always_call": {"test": {"realized_return_per_unit_staked": 0.0,
                                  "combined_win_rate": 0.5}}},
        min_observations=30,
    )
    assert "insufficient" in thin["verdict"]


def test_evaluation_never_imported_by_live_pipeline():
    import inspect

    import binary_options_selection.pipelines.run_binary_options_selection as pipe
    assert "evaluation" not in inspect.getsource(pipe)
