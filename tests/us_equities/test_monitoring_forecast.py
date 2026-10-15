"""Forecasting and its validation.

The validation is the part that keeps the monitor honest, so it is tested from
both sides: it must not find "skill" in pure noise more often than chance
allows, and it must find skill when a model genuinely has it (an oracle that
peeks at the future). A test that could do neither would be decoration.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from asset_selection.monitoring.config import ForecastConfig
from asset_selection.monitoring.forecast import (
    BootstrapForecaster,
    ChronosForecaster,
    Forecaster,
    cdf_at,
    diebold_mariano,
    forecast_series,
    holm,
    student_t_cdf,
    walk_forward_skill,
    wilson,
)

from .monitoring_fixtures import price_frame


def _cfg(**kw) -> ForecastConfig:
    base = dict(n_paths=600, validation_origins=24, validation_step=21,
                horizons=[21, 63, 126], min_history_days=300)
    base.update(kw)
    return ForecastConfig(**base)


def _series(seed=1, n=1200, sigma=0.015) -> np.ndarray:
    return price_frame(n, sigma=sigma, seed=seed)["adj_close"].to_numpy()


class OracleForecaster(Forecaster):
    """Knows the realised future: a tight fan centred on what actually happened."""

    name = "oracle"
    directional = True

    def __init__(self, series: np.ndarray, spread: float = 0.002) -> None:
        self.series = series
        self.spread = spread
        self.where = {float(v): i for i, v in enumerate(series)}

    def quantile_grids(self, contexts, horizon, levels, *, key=""):
        z = np.array([math.sqrt(2) * _erfinv(2 * q - 1) for q in levels])
        out = []
        for c in contexts:
            o = self.where[float(c[-1])]
            future = self.series[o + 1: o + 1 + horizon]
            out.append(future[:, None] * np.exp(self.spread * z)[None, :])
        return np.stack(out)


def _erfinv(y: float) -> float:
    from statistics import NormalDist
    return NormalDist().inv_cdf((y + 1) / 2) / math.sqrt(2)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("t, df, expected", [
    (0.0, 5, 0.5),
    (1.0, 1, 0.75),             # Cauchy
    (2.228, 10, 0.975),
    (-2.228, 10, 0.025),
    (1.96, 10_000, 0.975),      # converges to the normal
])
def test_student_t_cdf(t, df, expected):
    assert student_t_cdf(t, df) == pytest.approx(expected, abs=1e-3)


def test_holm_adjustment():
    adj = holm({21: 0.01, 63: 0.04, 126: 0.03})
    assert adj[21] == pytest.approx(0.03)        # 3 * 0.01
    assert adj[126] == pytest.approx(0.06)       # 2 * 0.03
    assert adj[63] == pytest.approx(0.06)        # max(1 * 0.04, previous) -- monotone
    assert holm({}) == {}


def test_diebold_mariano_direction():
    better = -np.abs(np.random.default_rng(0).normal(0.5, 0.1, 30))
    stat, p = diebold_mariano(better, lag=0)
    assert stat < 0 and p < 0.01
    stat, p = diebold_mariano(-better, lag=0)
    assert stat > 0 and p > 0.99
    assert diebold_mariano(np.zeros(10), lag=0) == (None, None)


def test_wilson_interval():
    lo, hi = wilson([1.0] * 12 + [0.0] * 12)
    assert lo < 0.5 < hi and 0.25 < lo and hi < 0.75


def test_cdf_is_bounded_by_the_grid():
    levels = [0.05, 0.5, 0.95]
    q = np.array([90.0, 100.0, 110.0])
    assert cdf_at(100.0, q, levels) == pytest.approx(0.5)
    assert cdf_at(50.0, q, levels) == 0.05 and cdf_at(500.0, q, levels) == 0.95


# ---------------------------------------------------------------------------
# Bootstrap backend
# ---------------------------------------------------------------------------

def test_bootstrap_is_deterministic_and_well_formed():
    cfg = _cfg()
    s = _series()
    a = BootstrapForecaster(cfg).quantile_grids([s], 63, cfg.quantile_levels, key="AAA")
    b = BootstrapForecaster(cfg).quantile_grids([s], 63, cfg.quantile_levels, key="AAA")
    assert a.shape == (1, 63, len(cfg.quantile_levels))
    np.testing.assert_allclose(a, b)
    assert np.all(np.diff(a[0], axis=1) >= 0)                   # quantiles never cross
    assert np.all(a[0, -1, -1] - a[0, -1, 0] > a[0, 0, -1] - a[0, 0, 0])   # the fan widens


def test_zero_drift_bootstrap_has_no_directional_view():
    cfg = _cfg(n_paths=4000)
    s = _series(sigma=0.02) * np.exp(np.linspace(0, 1.5, 1200))     # a strongly trending history
    fc = BootstrapForecaster(cfg)
    assert fc.directional is False
    paths = fc.paths(s, 63, key="T")
    assert np.median(paths[:, -1]) == pytest.approx(s[-1], rel=0.03)   # the trend is NOT extrapolated


def test_annual_drift_option_moves_the_median():
    cfg = _cfg(n_paths=4000, bootstrap_drift=0.25)
    s = _series()
    paths = BootstrapForecaster(cfg).paths(s, 252, key="D")
    assert np.median(paths[:, -1]) / s[-1] == pytest.approx(1.25, rel=0.06)


# ---------------------------------------------------------------------------
# forecast_series
# ---------------------------------------------------------------------------

def test_forecast_outputs_horizons_targets_and_notes():
    cfg = _cfg()
    s = _series()
    p0 = float(s[-1])
    res = forecast_series(s, cfg, BootstrapForecaster(cfg), key="AAA", as_of="2026-10-13",
                          targets=[("break-even", p0 * 1.3), ("+10%", p0 * 1.1)])
    assert res.status == "ok" and res.directional is False
    assert [h.horizon_days for h in res.horizons] == [21, 63, 126]
    for t in res.targets:
        for h in (21, 63, 126):
            # Touching a level at some point is always at least as likely as ending above it.
            assert t.p_touch_by[h] >= t.p_above_at[h] - 1e-12
        assert t.median_crossing_day is None               # a flat median never reaches an up-target
    # Further-away targets are less likely.
    be, ten = res.targets
    assert be.p_above_at[126] < ten.p_above_at[126]
    assert any("No directional view" in n for n in res.notes)


def test_insufficient_history_is_reported_not_forecast():
    res = forecast_series(_series()[:100], _cfg(), BootstrapForecaster(_cfg()), key="X")
    assert res.status == "insufficient_history" and not res.horizons


def test_backend_failure_is_captured():
    class Broken(Forecaster):
        name = "broken"
        def quantile_grids(self, *a, **k):
            raise RuntimeError("model exploded")
    res = forecast_series(_series(), _cfg(validate=False), Broken(), key="X")
    assert res.status == "error" and "model exploded" in res.error


# ---------------------------------------------------------------------------
# Walk-forward validation: both directions
# ---------------------------------------------------------------------------

def test_validation_does_not_invent_skill_in_pure_noise():
    """On random walks the bootstrap IS (nearly) the baseline: no seed should look skilled."""
    cfg = _cfg(n_paths=400)
    false_positives = 0
    for seed in range(8):
        s = _series(seed=100 + seed)
        rep = walk_forward_skill(s, cfg, BootstrapForecaster(cfg), key=f"N{seed}")
        false_positives += sum(1 for r in rep.values() if r.informative)
    assert false_positives <= 1


def test_validation_detects_a_model_that_really_knows():
    cfg = _cfg()
    s = _series(seed=7)
    rep = walk_forward_skill(s, cfg, OracleForecaster(s), key="O")
    assert rep[21].informative and rep[21].skill > 0.5
    assert rep[63].informative
    assert rep[21].hit_rate > 0.9


def test_too_few_independent_windows_can_never_be_informative():
    """24 windows stepped 21 days apart hold ~4 independent 126-day outcomes."""
    cfg = _cfg()
    s = _series(seed=7)
    rep = walk_forward_skill(s, cfg, OracleForecaster(s), key="O")
    assert rep[126].effective_windows == pytest.approx(4.0)
    assert rep[126].skill > 0.5 and not rep[126].informative
    assert rep[126].p_value_adjusted is None


def test_validation_needs_enough_origins():
    cfg = _cfg()
    assert walk_forward_skill(_series()[:420], cfg, BootstrapForecaster(cfg), key="S") == {}


# ---------------------------------------------------------------------------
# Chronos backend
# ---------------------------------------------------------------------------

def test_chronos_is_directional_and_respects_trained_quantiles():
    two = ChronosForecaster(_cfg(chronos_model="amazon/chronos-2"))
    bolt = ChronosForecaster(_cfg(chronos_model="amazon/chronos-bolt-small"))
    levels = [0.05, 0.1, 0.5, 0.9, 0.95]
    assert two.directional is True
    assert two.supported_levels(levels) == levels
    # Bolt silently clamps outside 0.1-0.9; never ask it for those levels.
    assert bolt.supported_levels(levels) == [0.1, 0.5, 0.9]


def test_chronos_backend_end_to_end_when_installed():
    pytest.importorskip("chronos")
    cfg = _cfg(backend="chronos", validation_origins=10)
    s = _series(seed=11)
    res = forecast_series(s, cfg, ChronosForecaster(cfg), key="C", targets=[("+10%", s[-1] * 1.1)])
    assert res.status == "ok", res.error
    assert res.directional and len(res.horizons) == 3
    q = res.horizons[-1].quantiles
    assert q[0.05] < q[0.5] < q[0.95]
    assert set(res.skill) == {21, 63, 126}



# ---------------------------------------------------------------------------
# Real-data failure modes, pinned
# ---------------------------------------------------------------------------

def test_benjamini_hochberg():
    from asset_selection.monitoring.forecast import benjamini_hochberg
    q = benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
    assert q["a"] == pytest.approx(0.04)
    assert q["c"] == pytest.approx(0.04 * 4 / 3)      # step-up: min(0.06, 0.0533)
    assert q["b"] == pytest.approx(0.04 * 4 / 3)
    assert q["d"] == pytest.approx(0.5)
    assert benjamini_hochberg({}) == {}


def _fc_with(p_values, skill=0.1, eff=24.0):
    from asset_selection.monitoring.models import ForecastResult, SkillReport
    fc = ForecastResult(backend="chronos", directional=True)
    for h, p in p_values.items():
        fc.skill[h] = SkillReport(h, 24, 0.9, 1.0, skill, -2.0, p, None, None, None,
                                  informative=True, effective_windows=eff)
    return fc


def test_one_lucky_stock_among_many_is_not_treated_as_predictable():
    """Seen on real data: 1 of 24 tests passed at p=0.0037 -- about what luck predicts."""
    from asset_selection.monitoring.forecast import apply_run_level_fdr
    results = [("MTZ", _fc_with({21: 0.0037, 63: 0.36}))]
    results += [(f"S{i}", _fc_with({21: 0.2 + 0.03 * i, 63: 0.3 + 0.02 * i})) for i in range(11)]
    assert apply_run_level_fdr(results, 0.05, 8.0) == 0
    mtz = results[0][1].skill[21]
    assert not mtz.informative and mtz.p_value_adjusted == pytest.approx(0.0037 * 24)


def test_a_genuine_edge_survives_the_run_level_correction():
    from asset_selection.monitoring.forecast import apply_run_level_fdr
    results = [("REAL", _fc_with({21: 1e-6, 63: 1e-5}))]
    results += [(f"S{i}", _fc_with({21: 0.5, 63: 0.6})) for i in range(11)]
    assert apply_run_level_fdr(results, 0.05, 8.0) == 2
    assert results[0][1].skill[21].informative and results[0][1].skill[63].informative


def test_run_level_correction_respects_the_window_guard():
    from asset_selection.monitoring.forecast import apply_run_level_fdr
    results = [("X", _fc_with({126: 1e-9}, eff=4.0))]
    assert apply_run_level_fdr(results, 0.05, 8.0) == 0
    assert results[0][1].skill[126].p_value_adjusted is None


class NegativeTailForecaster(Forecaster):
    """Like Chronos at long horizons: a low quantile can dip below zero."""

    name = "negtail"
    directional = True

    def quantile_grids(self, contexts, horizon, levels, *, key=""):
        out = []
        for c in contexts:
            p0 = float(c[-1])
            row = np.array([p0 * (1 + 3.0 * (q - 0.5)) for q in levels])   # q=0.05 -> negative
            out.append(np.tile(row, (horizon, 1)))
        return np.stack(out)


def test_non_positive_quantiles_never_turn_validation_into_nan():
    """Seen on real data (ARES): a negative quantile made log() NaN and erased the test."""
    cfg = _cfg()
    s = _series(seed=21)
    rep = walk_forward_skill(s, cfg, NegativeTailForecaster(), key="NEG")
    for r in rep.values():
        assert r.skill is not None and np.isfinite(r.skill)
        assert np.isfinite(r.loss_model)
    res = forecast_series(s, cfg, NegativeTailForecaster(), key="NEG")
    assert all(v > 0 for h in res.horizons for v in h.quantiles.values())
    assert any("clipped" in n for n in res.notes)
