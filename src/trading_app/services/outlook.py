"""Price outlook for open positions: 1, 3, 6 and 12 months, and a 1-month exit plan.

**Model.** The monitor's stationary block bootstrap
(:mod:`asset_selection.monitoring.forecast`) -- 2,000 simulated price paths
built from the stock's own past daily moves -- with one change: instead of
removing the stock's trend entirely, it keeps it **shrunk by how reliable it
is**. The average daily move over the last three years is multiplied by
``max(0, 1 - 1/t^2)`` (positive-part James-Stein shrinkage; ``t`` is that
average's t-statistic), so a trend that is statistically weak counts for
little or nothing and a strong one counts nearly in full.

**Per horizon** (21, 63, 126 and 252 trading days):

* trend -- up when at least 55% of the paths end above today's price, down
  when at most 45% do, flat in between;
* average -- the mean simulated price at the horizon;
* min / max -- the average of each path's lowest and highest close up to the
  horizon.

**Honesty.** Every trend is back-tested walk-forward on the stock's own past
(no look-ahead: each test month uses only prices before it): how often the
predicted direction was right, and whether that is better than a coin flip
with 95% confidence. Overlapping windows carry little independent evidence
(24 monthly tests of a 1-year call are about two independent years), so a
horizon with fewer than eight independent windows is reported as "not
testable" rather than validated.

**Short-term recommendation (1 month).** For each lot, sixty sell prices
between today's and the top of the simulated month are tried; each scores
its **price improvement over selling now times the chance of reaching it
within the month**. The highest score among limits reached in at least a
quarter of the simulated months is the recommended limit sell: the best
trade-off between a better price and actually getting it (higher limits pay
more but fill less often). A protective stop is placed where the
price falls in about one month in ten. The trend enters this choice only if
it passed its back-test at one month; otherwise the paths carry no trend at
all, so an unproven forecast never moves the recommendation. When even the
best limit adds less than 0.5% to selling now, the recommendation is to sell
now. Closing prices only, and a stop is assumed to fill at its price (a gap
can fill lower).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from asset_selection.monitoring.config import ForecastConfig
from asset_selection.monitoring.forecast import BootstrapForecaster, walk_forward_skill

HORIZONS: Dict[int, str] = {21: "1M", 63: "3M", 126: "6M", 252: "1Y"}
MIN_DAYS = 60                # fewer daily closes than this: no outlook at all
UP, DOWN = 0.55, 0.45        # P(up) thresholds for calling a trend
MIN_INDEPENDENT = 8.0        # independent test windows needed before a trend can count as validated
STOP_TOUCH = 0.10            # protective stop: reached in about one simulated month in ten
MIN_IMPROVEMENT = 0.005      # a limit must add at least 0.5% over selling now to be worth waiting for
MIN_FILL = 0.25              # a limit reached in fewer than a quarter of months is not a usable plan


class TrendBootstrap(BootstrapForecaster):
    """The monitor's block bootstrap, keeping the stock's own trend shrunk by its reliability."""

    name = "trend-bootstrap"
    directional = True

    def __init__(self, cfg: ForecastConfig) -> None:
        super().__init__(cfg)
        self.model_id = (f"stationary-bootstrap(L={self.block}, window={self.window}d, "
                         f"drift=own trend x max(0, 1-1/t^2))")

    def drift(self, context: np.ndarray) -> Tuple[float, float, float]:
        """(raw daily drift, t-statistic, shrunk daily drift) of the context's log returns."""
        prices = np.asarray(context, dtype=float)
        r = np.diff(np.log(prices[-(self.window + 1):]))
        r = r[np.isfinite(r)]
        if r.size < 20:
            return 0.0, 0.0, 0.0
        m, sd = float(r.mean()), float(r.std(ddof=1))
        t = m / (sd / math.sqrt(r.size)) if sd > 0 else 0.0
        shrink = max(0.0, 1.0 - 1.0 / (t * t)) if t else 0.0
        return m, t, m * shrink

    def paths(self, context, horizon, *, key: str = "", salt: int = 0) -> np.ndarray:
        self.daily_drift = self.drift(context)[2]      # the parent demeans, then adds this drift back
        return super().paths(context, horizon, key=key, salt=salt)


def _cfg(horizons, n_paths: int = 2000) -> ForecastConfig:
    return ForecastConfig(horizons=list(horizons), n_paths=n_paths)


def stock_outlook(bars: pd.DataFrame, *, key: str) -> Dict[str, Any]:
    """Trend, average, min and max at each horizon, with each trend's back-test."""
    closes = bars["close"].to_numpy(dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    as_of = bars.index[-1].strftime("%Y-%m-%d") if len(bars) else None
    if closes.size < MIN_DAYS:
        return {"status": "insufficient_history", "as_of": as_of, "history_days": int(closes.size)}
    model = TrendBootstrap(_cfg(HORIZONS))
    context = closes[-(model.window + 1):]
    raw, t, used = model.drift(context)
    paths = model.paths(context, max(HORIZONS), key=key)
    p0 = float(closes[-1])
    out: Dict[str, Any] = {
        "status": "ok", "as_of": as_of, "last_close": p0, "history_days": int(closes.size),
        "model": model.model_id,
        "trend_per_year_raw_pct": (math.exp(raw * 252) - 1) * 100,
        "trend_per_year_used_pct": (math.exp(used * 252) - 1) * 100,
        "trend_t_stat": t,
        "horizons": {},
    }
    check = TrendBootstrap(_cfg([21], n_paths=500))              # lighter model for the back-tests
    for h, label in HORIZONS.items():
        end = paths[:, h - 1]
        p_up = float(np.mean(end > p0))
        row: Dict[str, Any] = {
            "label": label, "days": h, "p_up": p_up,
            # No drift left after shrinkage = no directional view, whatever sampling noise says.
            "trend": "flat" if used == 0 else "up" if p_up >= UP else "down" if p_up <= DOWN else "flat",
            "average": float(end.mean()), "median": float(np.median(end)),
            "min": float(paths[:, :h].min(axis=1).mean()), "max": float(paths[:, :h].max(axis=1).mean()),
            "tests": None, "hits": None, "hit_rate": None, "independent_windows": None,
            "testable": False, "validated": False, "range_held": None,
        }
        try:
            rep = walk_forward_skill(closes, _cfg([h], n_paths=500), check, key=key).get(h)
        except Exception:  # noqa: BLE001 - an untested trend is shown as untested
            rep = None
        if rep is not None and rep.hit_rate is not None:
            indep = float(rep.effective_windows or 0)
            ci_low = (rep.hit_rate_ci or [0, 0])[0]
            row.update(tests=int(rep.n_origins), hits=int(round(rep.hit_rate * rep.n_origins)),
                       hit_rate=float(rep.hit_rate), independent_windows=indep,
                       testable=indep >= MIN_INDEPENDENT,
                       validated=bool(indep >= MIN_INDEPENDENT and ci_low > 0.5 and row["trend"] != "flat"),
                       range_held=rep.coverage_80)
        out["horizons"][str(h)] = row
    return out


def month_paths(bars: pd.DataFrame, *, key: str, with_trend: bool) -> Optional[np.ndarray]:
    """2,000 simulated next months (21 trading days); with the shrunk trend only if it is validated."""
    closes = bars["close"].to_numpy(dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    if closes.size < MIN_DAYS:
        return None
    model = TrendBootstrap(_cfg(HORIZONS)) if with_trend else BootstrapForecaster(_cfg(HORIZONS))
    return model.paths(closes[-(model.window + 1):], 21, key=key)


def exit_plan(paths: np.ndarray, *, price: float, cost_per_share: float, shares: float,
              commission_rate: float, trend_used: bool) -> Dict[str, Any]:
    """The sell price with the best trade-off between price and the chance of reaching it in a month."""
    keep = 1.0 - commission_rate
    run_max, run_min = paths.max(axis=1), paths.min(axis=1)
    top = float(np.quantile(run_max, 0.99))
    grid = np.linspace(price, max(top, price), 61)[1:]
    fill = np.array([float(np.mean(run_max >= level)) for level in grid])
    gain = (grid - price) * fill                         # expected improvement per share over selling now
    gain = np.where(fill >= MIN_FILL, gain, -np.inf)     # only limits with a real chance of filling
    k = int(np.argmax(gain))
    limit, p_fill = float(grid[k]), float(fill[k])
    stop = float(np.quantile(run_min, STOP_TOUCH))
    sell_now = gain[k] < MIN_IMPROVEMENT * price

    def vs_cost(px: float) -> Optional[float]:
        return (px * keep / cost_per_share - 1) * 100 if cost_per_share else None

    exit_price = price if sell_now else limit
    return {
        "plan": "sell_now" if sell_now else "limit",
        "limit": None if sell_now else limit, "p_fill": 1.0 if sell_now else p_fill,
        "stop": None if sell_now else stop, "p_stop": None if sell_now else float(np.mean(run_min <= stop)),
        "price": price,
        "exit_vs_cost_pct": vs_cost(exit_price), "now_vs_cost_pct": vs_cost(price),
        "stop_vs_cost_pct": None if sell_now else vs_cost(stop),
        "exit_pl": (exit_price * keep - cost_per_share) * shares,
        "now_pl": (price * keep - cost_per_share) * shares,
        "expected_improvement": float(gain[k]) * shares,          # over selling now, before it fills or not
        "break_even": cost_per_share / keep,
        "trend_used": trend_used,
        "why": ("even the best limit adds less than 0.5% over today's price within a month"
                if sell_now else
                "best trade-off between a higher price and the chance of reaching it within a month"),
        "confidence": "uses a validated trend" if trend_used else "no proven trend: volatility only",
    }
