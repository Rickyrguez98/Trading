"""Probabilistic price forecasts -- and an honest test of whether they help.

What this module does **not** claim: precise price predictions. Published
evaluations of pretrained time-series models on stock returns (e.g. Noguer i
Alonso & Pereira Franklin, arXiv:2606.27100, 2026) find "gains over the
random-walk benchmark are small and sparse". So every forecast here is:

1. **Probabilistic** -- a fan of quantiles per trading day, from which the
   report derives P(up), P(above a target at a horizon), the first day the
   forecast's median crosses a target, and (for path models) the probability
   of touching a target at any point.
2. **Validated per ticker** with a walk-forward backtest against a zero-drift
   random walk whose volatility is estimated the same way. The score is the
   mean pinball (quantile) loss, a standard approximation of CRPS; a one-sided
   Diebold-Mariano test with the Harvey-Leybourne-Newbold correction and a
   Newey-West variance (forecast windows overlap) decides whether the model
   is *significantly* better. Only then is the forecast called informative,
   and only then does :mod:`.decision` let it influence an action.

Backends
--------
* ``bootstrap`` -- stationary block bootstrap of the stock's own daily log
  returns (fat tails and volatility clustering preserved). By default it is
  demeaned: **no directional view**, so its median stays at today's price and
  its probabilities describe volatility, not an edge. Fast, no dependencies.
* ``chronos`` -- Amazon's pretrained Chronos-2 transformer (zero-shot, ~120M
  parameters, tens of milliseconds per forecast on CPU). Optional:
  ``pip install '.[forecast]'`` on Python >= 3.10.
"""
from __future__ import annotations

import logging
import math
import zlib
from abc import ABC, abstractmethod
from statistics import NormalDist
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import ForecastConfig
from .models import ForecastResult, HorizonForecast, SkillReport, TargetOutlook

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
_BASELINE_VOL_WINDOW = 252
_MIN_CONTEXT = 120


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Forecaster(ABC):
    name: str = "base"
    model_id: Optional[str] = None
    produces_paths: bool = False
    # Whether the model can express a view on direction at all. A model that
    # cannot (by construction) never feeds P(up) into a decision.
    directional: bool = False

    def supported_levels(self, levels: Sequence[float]) -> List[float]:
        return list(levels)

    @abstractmethod
    def quantile_grids(
        self, contexts: Sequence[np.ndarray], horizon: int, levels: Sequence[float], *, key: str = ""
    ) -> np.ndarray:
        """(n_contexts, horizon, n_levels) price quantiles for days 1..horizon."""

    def paths(self, context: np.ndarray, horizon: int, *, key: str = "") -> Optional[np.ndarray]:
        """(n_paths, horizon) simulated prices, for backends that simulate."""
        return None


class BootstrapForecaster(Forecaster):
    name = "bootstrap"
    produces_paths = True

    def __init__(self, cfg: ForecastConfig) -> None:
        self.n_paths = int(cfg.n_paths)
        self.block = max(1, int(cfg.block_length))
        self.window = int(cfg.bootstrap_window_days)
        self.seed = int(cfg.seed)
        drift = cfg.bootstrap_drift
        self.daily_drift = 0.0 if drift == "zero" else math.log1p(float(drift)) / TRADING_DAYS
        self.model_id = (
            f"stationary-bootstrap(L={self.block}, window={self.window}d, "
            f"drift={'none' if drift == 'zero' else f'{100 * float(drift):.1f}%/yr'})"
        )

    def _rng(self, key: str, salt: int = 0) -> np.random.Generator:
        return np.random.default_rng(self.seed + zlib.crc32(key.encode()) + salt)

    def paths(self, context: np.ndarray, horizon: int, *, key: str = "", salt: int = 0) -> np.ndarray:
        prices = np.asarray(context, dtype=float)
        r = np.diff(np.log(prices[-(self.window + 1):]))
        r = r[np.isfinite(r)]
        if r.size < 20:
            raise ValueError("not enough returns to bootstrap")
        r = r - r.mean()                          # remove the sample's own drift
        n = r.size
        rng = self._rng(key, salt)
        idx = np.empty((self.n_paths, horizon), dtype=np.int64)
        idx[:, 0] = rng.integers(0, n, self.n_paths)
        restart = rng.random((self.n_paths, horizon)) < (1.0 / self.block)
        fresh = rng.integers(0, n, (self.n_paths, horizon))
        for t in range(1, horizon):
            nxt = (idx[:, t - 1] + 1) % n
            idx[:, t] = np.where(restart[:, t], fresh[:, t], nxt)
        steps = r[idx] + self.daily_drift
        return prices[-1] * np.exp(np.cumsum(steps, axis=1))

    def quantile_grids(self, contexts, horizon, levels, *, key: str = "") -> np.ndarray:
        out = []
        for i, ctx in enumerate(contexts):
            p = self.paths(ctx, horizon, key=key, salt=i)
            out.append(np.quantile(p, levels, axis=0).T)
        return np.stack(out)


class ChronosForecaster(Forecaster):
    name = "chronos"
    directional = True

    def __init__(self, cfg: ForecastConfig) -> None:
        self.model_id = cfg.chronos_model
        self.context_days = int(cfg.context_days)
        self._pipe = None

    def _pipeline(self):
        if self._pipe is None:
            try:
                import torch
                from chronos import BaseChronosPipeline
            except ImportError as exc:
                raise ImportError(
                    "the chronos backend needs chronos-forecasting (Python >= 3.10): "
                    "pip install '.[forecast]'"
                ) from exc
            self._pipe = BaseChronosPipeline.from_pretrained(
                self.model_id, device_map="cpu", dtype=torch.float32
            )
        return self._pipe

    def supported_levels(self, levels: Sequence[float]) -> List[float]:
        # Chronos-Bolt was trained on 0.1..0.9 and silently clamps anything
        # outside; requesting 0.05 would return the 0.10 value relabelled.
        lo, hi = (0.1, 0.9) if "bolt" in str(self.model_id).lower() else (0.01, 0.99)
        kept = [q for q in levels if lo - 1e-9 <= q <= hi + 1e-9]
        return kept if 0.5 in kept else sorted(set(kept) | {0.5})

    def quantile_grids(self, contexts, horizon, levels, *, key: str = "") -> np.ndarray:
        import torch

        pipe = self._pipeline()
        tensors = [torch.tensor(np.asarray(c, dtype=np.float32)[-self.context_days:]) for c in contexts]
        q, _mean = pipe.predict_quantiles(tensors, prediction_length=int(horizon),
                                          quantile_levels=list(levels))
        if isinstance(q, list):
            arr = np.stack([x.detach().cpu().numpy().reshape(int(horizon), len(levels)) for x in q])
        else:
            arr = q.detach().cpu().numpy().reshape(len(tensors), int(horizon), len(levels))
        return arr.astype(float)


def make_forecaster(cfg: ForecastConfig) -> Forecaster:
    if cfg.backend == "chronos":
        return ChronosForecaster(cfg)
    return BootstrapForecaster(cfg)


# ---------------------------------------------------------------------------
# Distribution helpers
# ---------------------------------------------------------------------------

def _monotone(grid: np.ndarray) -> np.ndarray:
    """Quantiles must not cross; enforce non-decreasing along the level axis."""
    return np.maximum.accumulate(grid, axis=-1)


_PRICE_FLOOR = 1e-4     # as a fraction of the reference price


def _positive(grid: np.ndarray, reference: float) -> Tuple[np.ndarray, int]:
    """Clip non-positive price quantiles to a tiny positive floor.

    A model that is not constrained to positive values (Chronos forecasts the
    level directly) can put a low quantile below zero at long horizons. A
    price cannot be negative, and ``log`` of one is NaN -- which would silently
    erase that stock's validation. The floor keeps the maths finite and makes
    the impossible forecast score badly, as it should.
    """
    floor = _PRICE_FLOOR * float(reference)
    bad = int(np.count_nonzero(~(grid > floor)))
    return (np.where(grid > floor, grid, floor) if bad else grid), bad


def cdf_at(x: float, quantiles: np.ndarray, levels: Sequence[float]) -> float:
    """P(X <= x) by linear interpolation of the quantile function.

    Bounded by the outermost levels: beyond them the grid cannot say more, so
    with levels 0.05..0.95 a reported 5% means "5% or less".
    """
    q = np.asarray(quantiles, dtype=float)
    lv = np.asarray(levels, dtype=float)
    if x <= q[0]:
        return float(lv[0])
    if x >= q[-1]:
        return float(lv[-1])
    return float(np.interp(x, q, lv))


def _level_index(levels: Sequence[float], q: float) -> int:
    return int(np.argmin(np.abs(np.asarray(levels) - q)))


# ---------------------------------------------------------------------------
# One forecast
# ---------------------------------------------------------------------------

def forecast_series(
    prices: np.ndarray,
    cfg: ForecastConfig,
    forecaster: Forecaster,
    *,
    key: str,
    as_of: Optional[str] = None,
    targets: Optional[List[Tuple[str, float]]] = None,
) -> ForecastResult:
    """Forecast one adjusted-close series and evaluate the requested targets."""
    res = ForecastResult(backend=forecaster.name, model_id=forecaster.model_id,
                         directional=forecaster.directional, as_of=as_of)
    prices = np.asarray(prices, dtype=float)
    prices = prices[np.isfinite(prices) & (prices > 0)]
    if prices.size < int(cfg.min_history_days):
        res.status = "insufficient_history"
        res.notes.append(f"{prices.size} daily closes < {cfg.min_history_days} required")
        return res
    levels = forecaster.supported_levels(cfg.quantile_levels)
    horizons = sorted({int(h) for h in cfg.horizons})
    max_h = max(horizons)
    p0 = float(prices[-1])
    res.last_price = p0
    try:
        context = prices[-max(int(cfg.context_days), int(cfg.bootstrap_window_days) + 1):]
        paths = forecaster.paths(context, max_h, key=key) if forecaster.produces_paths else None
        if paths is not None:
            # One simulation feeds both the quantile fan and the touch probabilities.
            grid = _monotone(np.quantile(paths, levels, axis=0).T)
        else:
            grid = _monotone(forecaster.quantile_grids([context], max_h, levels, key=key)[0])
    except Exception as exc:  # noqa: BLE001 - reported on the position, never fatal
        res.status, res.error = "error", f"{type(exc).__name__}: {exc}"
        return res
    grid, clipped = _positive(grid, p0)
    if clipped:
        res.notes.append(f"The model put {clipped} price quantile(s) at or below zero; they were "
                         "clipped to a minimal positive price.")

    for h in horizons:
        row = grid[h - 1]
        p_up = (float(np.mean(paths[:, h - 1] > p0)) if paths is not None
                else 1.0 - cdf_at(p0, row, levels))
        res.horizons.append(HorizonForecast(
            horizon_days=h, quantiles={float(lv): float(v) for lv, v in zip(levels, row)}, p_up=p_up,
        ))

    med = _level_index(levels, 0.5)
    for label, target in targets or []:
        if target <= 0:
            continue
        above = {}
        for h in horizons:
            if paths is not None:
                above[h] = float(np.mean(paths[:, h - 1] >= target))
            else:
                above[h] = 1.0 - cdf_at(target, grid[h - 1], levels)
        crossing = None
        hits = np.nonzero(grid[:, med] >= target)[0] if target >= p0 else np.nonzero(grid[:, med] <= target)[0]
        if hits.size:
            crossing = int(hits[0]) + 1
        touch = None
        if paths is not None:
            reached = (paths >= target) if target >= p0 else (paths <= target)
            touch = {h: float(np.mean(reached[:, :h].any(axis=1))) for h in horizons}
        res.targets.append(TargetOutlook(label=label, target_price=float(target), p_above_at=above,
                                         median_crossing_day=crossing, p_touch_by=touch))

    if forecaster.name == "bootstrap" and getattr(forecaster, "daily_drift", None) == 0.0:
        res.notes.append(
            "No directional view: the bootstrap is demeaned, so its median stays at today's "
            "price and its probabilities describe volatility only."
        )
    if len(levels) < len(cfg.quantile_levels):
        res.notes.append(f"Quantile levels limited to {levels[0]}-{levels[-1]} by the model.")

    if cfg.validate:
        try:
            res.skill = walk_forward_skill(prices, cfg, forecaster, key=key, levels=levels)
        except Exception as exc:  # noqa: BLE001 - validation failure != forecast failure
            res.notes.append(f"validation failed: {type(exc).__name__}: {exc}")
        if res.skill and not any(s.informative for s in res.skill.values()):
            res.notes.append(
                "Not better than a random walk at any horizon in walk-forward testing; "
                "treat these numbers as a description of risk, not a prediction."
            )
        elif res.skill and not forecaster.directional:
            res.notes.append(
                "Its risk bands scored better than a random walk's, which says the spread of "
                "outcomes is well calibrated -- not that the direction is predictable."
            )
    return res


# ---------------------------------------------------------------------------
# Walk-forward validation
# ---------------------------------------------------------------------------

def walk_forward_skill(
    prices: np.ndarray,
    cfg: ForecastConfig,
    forecaster: Forecaster,
    *,
    key: str,
    levels: Optional[Sequence[float]] = None,
) -> Dict[int, SkillReport]:
    levels = list(levels or forecaster.supported_levels(cfg.quantile_levels))
    horizons = sorted({int(h) for h in cfg.horizons})
    max_h = max(horizons)
    n = prices.size
    step = int(cfg.validation_step)
    ctx_len = max(int(cfg.context_days), int(cfg.bootstrap_window_days) + 1)

    origins = [n - 1 - max_h - k * step for k in range(int(cfg.validation_origins))]
    origins = sorted(o for o in origins if o + 1 >= max(_MIN_CONTEXT, _BASELINE_VOL_WINDOW + 1))
    if len(origins) < 8:
        return {}
    contexts = [prices[max(0, o + 1 - ctx_len): o + 1] for o in origins]
    grids = _monotone(forecaster.quantile_grids(contexts, max_h, levels, key=key + "|wf"))
    for i, o in enumerate(origins):
        grids[i], _ = _positive(grids[i], prices[o])

    lv = np.asarray(levels)
    z = np.array([NormalDist().inv_cdf(q) for q in levels])
    reports: Dict[int, SkillReport] = {}
    for h in horizons:
        lm, lb, hits, inside = [], [], [], []
        for i, o in enumerate(origins):
            y = math.log(prices[o + h] / prices[o])
            q_model = np.log(grids[i, h - 1] / prices[o])
            r = np.diff(np.log(prices[max(0, o - _BASELINE_VOL_WINDOW): o + 1]))
            sigma = float(np.std(r, ddof=1)) if r.size > 2 else 0.0
            q_base = sigma * math.sqrt(h) * z
            lm.append(_pinball(y, q_model, lv))
            lb.append(_pinball(y, q_base, lv))
            med = q_model[_level_index(levels, 0.5)]
            if med != 0 and y != 0:
                hits.append(1.0 if (med > 0) == (y > 0) else 0.0)
            lo, hi = _level_index(levels, 0.1), _level_index(levels, 0.9)
            if abs(lv[lo] - 0.1) < 1e-9 and abs(lv[hi] - 0.9) < 1e-9:
                inside.append(1.0 if q_model[lo] <= y <= q_model[hi] else 0.0)
        lm_a, lb_a = np.asarray(lm), np.asarray(lb)
        mean_b = float(lb_a.mean())
        skill = 1.0 - float(lm_a.mean()) / mean_b if mean_b > 0 else 0.0
        lag = max(0, math.ceil(h / step) - 1)
        dm, p = diebold_mariano(lm_a - lb_a, lag)
        hit = float(np.mean(hits)) if hits else None
        reports[h] = SkillReport(
            horizon_days=h, n_origins=len(origins),
            loss_model=float(lm_a.mean()), loss_baseline=mean_b, skill=skill,
            dm_stat=dm, p_value=p,
            hit_rate=hit, hit_rate_ci=wilson(hits) if hits else None,
            coverage_80=float(np.mean(inside)) if inside else None,
            informative=False,
            effective_windows=min(float(len(origins)), len(origins) * step / float(h)),
        )

    # One claim ("this model beats a random walk for this stock") is tested at
    # several horizons, so the p-values are Holm-adjusted together; a horizon
    # with too few independent windows cannot carry the claim at all.
    alpha = float(cfg.validation_alpha)
    testable = [h for h, r in reports.items()
                if r.p_value is not None and (r.effective_windows or 0) >= float(cfg.min_effective_windows)]
    adjusted = holm({h: reports[h].p_value for h in testable})
    for h, rep in reports.items():
        rep.p_value_adjusted = adjusted.get(h)
        rep.informative = bool(h in adjusted and rep.skill > 0 and adjusted[h] < alpha)
    return reports


def benjamini_hochberg(pvalues: Dict) -> Dict:
    """Benjamini-Hochberg q-values (step-up, monotone, capped at 1)."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: Dict = {}
    running = 1.0
    for rank in range(m, 0, -1):
        k, p = items[rank - 1]
        running = min(running, p * m / rank)
        out[k] = min(1.0, running)
    return out


def apply_run_level_fdr(results: Sequence[Tuple[str, ForecastResult]], alpha: float,
                        min_effective_windows: float) -> int:
    """Re-decide ``informative`` across every stock and horizon in one run.

    Monitoring twelve stocks at three horizons is dozens of tests; at the 5%
    level one or two would "beat the random walk" by luck alone. Controlling
    the false-discovery rate across the whole run (Benjamini-Hochberg) keeps a
    lucky stock from being treated as predictable. Returns how many survive.
    """
    raw = {}
    for key, fc in results:
        for h, rep in fc.skill.items():
            if rep.p_value is not None and (rep.effective_windows or 0) >= float(min_effective_windows):
                raw[(key, h)] = rep.p_value
    q = benjamini_hochberg(raw)
    survivors = 0
    for key, fc in results:
        for h, rep in fc.skill.items():
            rep.p_value_adjusted = q.get((key, h))
            rep.informative = bool((key, h) in q and rep.skill > 0 and q[(key, h)] < alpha)
            survivors += rep.informative
    return survivors


def holm(pvalues: Dict[int, float]) -> Dict[int, float]:
    """Holm-Bonferroni adjusted p-values (monotone, capped at 1)."""
    items = sorted(pvalues.items(), key=lambda kv: kv[1])
    m = len(items)
    out: Dict[int, float] = {}
    running = 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def _pinball(y: float, q: np.ndarray, levels: np.ndarray) -> float:
    """Mean quantile loss across levels, x2 so it approximates CRPS."""
    diff = y - q
    return float(2.0 * np.mean(np.maximum(levels * diff, (levels - 1.0) * diff)))


def diebold_mariano(d: np.ndarray, lag: int) -> Tuple[Optional[float], Optional[float]]:
    """One-sided DM test that ``d = loss_model - loss_baseline`` has a negative mean.

    Newey-West variance with ``lag`` lags (overlapping windows are
    autocorrelated), Harvey-Leybourne-Newbold small-sample correction, and a
    Student-t reference with n-1 degrees of freedom.
    """
    d = np.asarray(d, dtype=float)
    n = d.size
    if n < 3:
        return None, None
    dc = d - d.mean()
    var = float(np.dot(dc, dc) / n)
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float(np.dot(dc[k:], dc[:-k]) / n)
    if var <= 0:
        return None, None
    dm = float(d.mean() / math.sqrt(var / n))
    hh = lag + 1
    corr = math.sqrt(max((n + 1 - 2 * hh + hh * (hh - 1) / n) / n, 1e-12))
    stat = dm * corr
    return stat, student_t_cdf(stat, n - 1)


def wilson(outcomes: Sequence[float], z: float = 1.96) -> List[float]:
    n = len(outcomes)
    if n == 0:
        return [0.0, 1.0]
    p = sum(outcomes) / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def student_t_cdf(t: float, df: int) -> float:
    """CDF of Student's t via the regularized incomplete beta function."""
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    tail = 0.5 * _betainc(0.5 * df, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 3e-14) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h
