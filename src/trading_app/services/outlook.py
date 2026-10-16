"""Next-month price range and sell levels for an open position.

Uses the monitor's forecast engine (:mod:`asset_selection.monitoring.forecast`):
a stationary block bootstrap of the stock's own daily returns, **demeaned**,
2,000 simulated paths over the next 21 trading days (about a month). It has
no view on direction -- the paths are centred on today's price -- so it
describes how far the price typically travels in a month, not where it will
go. From the paths:

* **expected high / low** -- the average of each path's highest and lowest
  close: the typical top and bottom of the coming month;
* **sell limit** -- the level the price reaches at some point in half of the
  simulated months (median of the path highs): a limit order there has about
  an even chance of filling within a month;
* **stop** -- the level the price falls to in a quarter of the simulated
  months (25th percentile of the path lows): a protective exit that is hit
  only in the weaker months.

Each outlook is checked the way the monitor checks its forecasts: a
walk-forward test over past months reports how often the real price ended
inside the model's 80% band (it should be about 80%) and whether the bands
beat a plain random walk. Closing prices only: intraday swings are wider, so
levels are touched somewhat more often than stated.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from asset_selection.monitoring.config import ForecastConfig
from asset_selection.monitoring.forecast import BootstrapForecaster, walk_forward_skill

HORIZON = 21                 # trading days, about one calendar month
MIN_DAYS = 60                # fewer daily closes than this: no outlook at all
SELL_LIMIT_TOUCH = 0.50      # share of simulated months that reach the sell limit
STOP_TOUCH = 0.25            # share of simulated months that fall to the stop


def month_outlook(bars: pd.DataFrame, *, key: str) -> Dict[str, Any]:
    """The coming month's range and sell levels for one symbol, from its daily bars."""
    closes = bars["close"].to_numpy(dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    as_of = bars.index[-1].strftime("%Y-%m-%d") if len(bars) else None
    if closes.size < MIN_DAYS:
        return {"status": "insufficient_history", "as_of": as_of, "history_days": int(closes.size)}
    cfg = ForecastConfig(horizons=[HORIZON])
    model = BootstrapForecaster(cfg)
    paths = model.paths(closes[-(cfg.bootstrap_window_days + 1):], HORIZON, key=key)
    highs, lows = paths.max(axis=1), paths.min(axis=1)
    sell_limit = float(np.quantile(highs, SELL_LIMIT_TOUCH))
    stop = float(np.quantile(lows, STOP_TOUCH))
    out: Dict[str, Any] = {
        "status": "ok",
        "as_of": as_of,
        "last_close": float(closes[-1]),
        "horizon_days": HORIZON,
        "history_days": int(closes.size),
        "expected_high": float(highs.mean()),
        "expected_low": float(lows.mean()),
        "high_p90": float(np.quantile(highs, 0.9)),
        "low_p10": float(np.quantile(lows, 0.1)),
        "sell_limit": sell_limit,
        "sell_limit_touch": float(np.mean(highs >= sell_limit)),
        "stop": stop,
        "stop_touch": float(np.mean(lows <= stop)),
        "model": model.model_id,
        "validated_months": None, "range_held_months": None, "coverage_80": None, "beats_random_walk": None,
    }
    if closes.size >= cfg.min_history_days:
        try:
            skill = walk_forward_skill(closes, cfg, model, key=key).get(HORIZON)
        except Exception:  # noqa: BLE001 - an outlook without its check is still shown, flagged
            skill = None
        if skill is not None and skill.coverage_80 is not None:
            out.update(validated_months=int(skill.n_origins), coverage_80=float(skill.coverage_80),
                       range_held_months=int(round(skill.coverage_80 * skill.n_origins)),
                       beats_random_walk=bool(skill.informative))
    return out


def lot_levels(outlook: Optional[Dict[str, Any]], cost_per_share: Optional[float]) -> Dict[str, Any]:
    """The sell levels measured against one lot's cost (incl. commission)."""
    if not outlook or outlook.get("status") != "ok" or not cost_per_share:
        return {}
    limit, stop = outlook["sell_limit"], outlook["stop"]
    return {
        "limit_vs_cost_pct": (limit / cost_per_share - 1) * 100,
        "stop_vs_cost_pct": (stop / cost_per_share - 1) * 100,
        "limit_above_cost": limit >= cost_per_share,
    }
