"""Price outlook for open positions, short term for volatile stocks and long term for the rest.

**Two styles.** A stock whose daily moves add up to at least 55% a year
(annualised volatility of the last year; the S&P 500 runs near 13-20%) is
analysed for the **short term** -- 1 week, 2 weeks, 1 month, 3 months -- with
a **swing plan** that trades its weekly range repeatedly. A steadier stock is
analysed for the **long term** -- 1, 3, 6 and 12 months -- with a 6-month exit
plan.

**Model.** The monitor's stationary block bootstrap
(:mod:`asset_selection.monitoring.forecast`) -- 2,000 simulated price paths
built from the stock's own past daily moves -- with one change: instead of
removing the stock's trend entirely, it keeps it **shrunk by how reliable it
is**. The average daily move over the last three years is multiplied by
``max(0, 1 - 1/t^2)`` (positive-part James-Stein shrinkage; ``t`` is that
average's t-statistic), so a trend that is statistically weak counts for
little or nothing and a strong one counts nearly in full.

**Per horizon:**

* trend -- up when at least 55% of the paths end above today's price, down
  when at most 45% do, flat in between;
* expected -- the median simulated price at the horizon (the mean is kept too);
* min / max -- the average of each path's lowest and highest close up to the
  horizon.

**Honesty.** Every trend is back-tested walk-forward on the stock's own past
(no look-ahead: each test month uses only prices before it), starting from
every month the history allows (up to ten years of tests): how often the
predicted direction was right, and whether that beats a coin flip with 95%
confidence. Overlapping windows carry less evidence than their count
suggests -- monthly tests of a 1-year call overlap by eleven months -- so the
confidence interval uses the number of *independent* windows, and a horizon
with fewer than eight is reported as "not testable" together with how many
years of prices it would need.

**Swing plan (short-term stocks).** Each week, bands are set around the
price at +/- 0.674 x (daily volatility of the last 60 days) x sqrt(5): the
distance a random walk reaches in about half of all weeks. Holding, sell when
the close reaches the upper band; out of the stock, buy back when it reaches
the lower band; the bands move with the price every week, and your commission
rate is paid on every trade. The rule is judged twice: on the 2,000 simulated
months, and on the stock's **real last two years**, month by month, against
simply holding. It is recommended only if it beat holding on average in those
real months (and called proven only if its winning share beats a coin flip
with 95% confidence); otherwise the recommendation falls back to a single
limit sell, and the page says swinging lost to holding.

**Trend rule (short-term stocks).** The opposite idea: hold while the close
is above its 20-day average, sell when it closes below, buy back when it
closes above again. Judged the same way on the real last two years. Of the
two rules, one is recommended only if it beat simply holding over those two
years (by at least 5 points) *and* on average month by month -- the one with
the larger lead; it is
chosen on the same two years it is judged on, so it is labelled unproven
unless its monthly record clears the 95% bar.

**Exit plan.** Sixty sell prices between today's and the top of the simulated
horizon (1 month for short-term stocks, 6 months for long-term ones) are
tried; each scores its **price improvement over selling now times the chance
of reaching it**. The highest score among limits reached in at least a quarter
of the simulations is the recommended limit sell. A protective stop is placed
where the price falls in about one simulation in ten. A trend enters only if
it passed its back-test at that horizon. When even the best limit adds less
than 0.5% to selling now, the recommendation is to sell now.

Closing prices only, and limits and stops are assumed to fill at their price
(a gap can fill better or worse).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from asset_selection.monitoring.config import ForecastConfig
from asset_selection.monitoring.forecast import BootstrapForecaster, walk_forward_skill

SHORT_TERM_VOL = 55.0        # % a year: at or above this, a stock is analysed for the short term
STYLES: Dict[str, Dict[str, Any]] = {
    "short": {"name": "Short term", "horizons": {5: "1W", 10: "2W", 21: "1M", 63: "3M"},
              "default": 21, "plan_days": 21, "plan_label": "a month"},
    "long": {"name": "Long term", "horizons": {21: "1M", 63: "3M", 126: "6M", 252: "1Y"},
             "default": 252, "plan_days": 126, "plan_label": "6 months"},
}
HORIZONS: Dict[int, str] = STYLES["long"]["horizons"]
ALL_LABELS = ["1W", "2W", "1M", "3M", "6M", "1Y"]
SWING_Z = 0.674              # a band a random walk reaches in about half of all weeks
SWING_DAYS = 5               # bands are reset every week
SWING_LOOKBACK = 60          # days of returns that set the band width
BACKTEST_DAYS = 504          # the swing rule is checked on the last two years, month by month
MIN_SWING_MONTHS = 12        # fewer real months than this: no verdict on the swing rule at all
TREND_SMA = 20               # trend rule: hold while the close is above its 20-day average
MIN_LEAD_PP = 5.0            # a rule must beat holding by at least 5 points over two years to be recommended
MIN_DAYS = 60                # fewer daily closes than this: no outlook at all
UP, DOWN = 0.55, 0.45        # P(up) thresholds for calling a trend
MIN_INDEPENDENT = 8.0        # independent test windows needed before a trend can count as validated
STEP = 21                    # one walk-forward test per month
MAX_TESTS = 120              # at most ten years of monthly tests
WARMUP = 253                 # the random-walk baseline needs a year of returns before the first test
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


def _cfg(horizons, n_paths: int = 2000, tests: int = 24) -> ForecastConfig:
    return ForecastConfig(horizons=list(horizons), n_paths=n_paths, validation_origins=tests,
                          validation_step=STEP)


def tests_possible(history_days: int, horizon: int) -> int:
    """How many monthly walk-forward tests a history of this length allows."""
    return max(0, min(MAX_TESTS, (history_days - WARMUP - horizon) // STEP + 1))


def years_needed(horizon: int) -> float:
    """Years of daily prices needed for MIN_INDEPENDENT independent tests at this horizon."""
    tests = math.ceil(MIN_INDEPENDENT * max(horizon, STEP) / STEP)
    return (WARMUP + horizon + STEP * (tests - 1)) / 252.0


def wilson_low(p: float, n: float, z: float = 1.96) -> float:
    """Lower end of the Wilson interval for a success rate ``p`` over ``n`` (possibly fractional) trials."""
    if n <= 0:
        return 0.0
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n)


def _closes(bars: pd.DataFrame) -> np.ndarray:
    closes = bars["close"].to_numpy(dtype=float)
    return closes[np.isfinite(closes) & (closes > 0)]


def annual_volatility_pct(closes: np.ndarray, days: int = 252) -> Optional[float]:
    r = np.diff(np.log(np.asarray(closes, dtype=float)[-(days + 1):]))
    return float(r.std(ddof=1) * math.sqrt(252) * 100) if r.size > 20 else None


def style_of(bars: pd.DataFrame) -> Tuple[str, Optional[float]]:
    """("short" or "long", annual volatility %): volatile stocks are analysed for the short term."""
    vol = annual_volatility_pct(_closes(bars))
    return ("short" if vol is not None and vol >= SHORT_TERM_VOL else "long"), vol


def stock_outlook(bars: pd.DataFrame, *, key: str, commission_rate: float = 0.0025) -> Dict[str, Any]:
    """Trend, expected, min and max per horizon (short or long term by volatility), with back-tests."""
    closes = _closes(bars)
    as_of = bars.index[-1].strftime("%Y-%m-%d") if len(bars) else None
    if closes.size < MIN_DAYS:
        return {"status": "insufficient_history", "as_of": as_of, "history_days": int(closes.size)}
    style, vol = style_of(bars)
    horizons = STYLES[style]["horizons"]
    model = TrendBootstrap(_cfg(horizons))
    context = closes[-(model.window + 1):]
    raw, t, used = model.drift(context)
    paths = model.paths(context, max(max(horizons), 252), key=key)
    p0 = float(closes[-1])
    out: Dict[str, Any] = {
        "status": "ok", "as_of": as_of, "last_close": p0, "history_days": int(closes.size),
        "style": style, "style_name": STYLES[style]["name"], "volatility_pct": vol,
        "model": model.model_id,
        "trend_per_year_raw_pct": (math.exp(raw * 252) - 1) * 100,
        "trend_per_year_used_pct": (math.exp(used * 252) - 1) * 100,
        "trend_t_stat": t,
        "horizons": {},
    }
    out["p_halve_1y"] = float(np.mean(paths[:, :252].min(axis=1) <= 0.5 * p0))
    out["median_path"] = [round(float(v), 4) for v in np.median(paths[:, :252], axis=0)]
    start = pd.Timestamp(as_of)
    check = TrendBootstrap(_cfg([21], n_paths=500))              # lighter model for the back-tests
    for h, label in horizons.items():
        end = paths[:, h - 1]
        p_up = float(np.mean(end > p0))
        row: Dict[str, Any] = {
            "label": label, "days": h, "p_up": p_up,
            # No drift left after shrinkage = no directional view, whatever sampling noise says.
            "trend": "flat" if used == 0 else "up" if p_up >= UP else "down" if p_up <= DOWN else "flat",
            "average": float(end.mean()), "median": float(np.median(end)),
            "min": float(paths[:, :h].min(axis=1).mean()), "max": float(paths[:, :h].max(axis=1).mean()),
            "date": (start + pd.offsets.BDay(h)).strftime("%Y-%m-%d"),
            "tests": None, "hits": None, "hit_rate": None, "independent_windows": None, "confidence_low": None,
            "testable": False, "validated": False, "range_held": None,
            "years_needed": round(years_needed(h), 1), "years_available": round(closes.size / 252.0, 1),
        }
        n_tests = tests_possible(int(closes.size), h)
        rep = None
        if n_tests >= 8:
            try:
                rep = walk_forward_skill(closes, _cfg([h], n_paths=500, tests=n_tests), check, key=key).get(h)
            except Exception:  # noqa: BLE001 - an untested trend is shown as untested
                rep = None
        if rep is not None and rep.hit_rate is not None:
            # Overlapping windows: judge the hit rate on the independent ones, not the raw count.
            indep = min(float(rep.n_origins), rep.n_origins * STEP / float(h))
            low = wilson_low(float(rep.hit_rate), indep)
            row.update(tests=int(rep.n_origins), hits=int(round(rep.hit_rate * rep.n_origins)),
                       hit_rate=float(rep.hit_rate), independent_windows=round(indep, 1), confidence_low=low,
                       testable=indep >= MIN_INDEPENDENT,
                       validated=bool(indep >= MIN_INDEPENDENT and low > 0.5 and row["trend"] != "flat"),
                       range_held=rep.coverage_80)
        out["horizons"][str(h)] = row
    if style == "short":
        trend_1m = bool(out["horizons"]["21"]["validated"])
        month = (paths if trend_1m else BootstrapForecaster(_cfg(horizons)).paths(context, 21, key=key))[:, :21]
        rules = {"range": swing_analysis(closes, month, commission_rate=commission_rate),
                 "trend": trend_analysis(closes, commission_rate=commission_rate)}
        out["rules"] = rules
        out["best_rule"] = best_rule(rules)
    return out


# ---------------------------------------------------------------------------
# Swing plan: trade the weekly range repeatedly (short-term stocks)
# ---------------------------------------------------------------------------

def _band(sigma_daily: np.ndarray) -> np.ndarray:
    return SWING_Z * sigma_daily * math.sqrt(SWING_DAYS)


def run_swing(prices: np.ndarray, sigmas: np.ndarray, *, commission_rate: float):
    """Apply the weekly band rule to price paths (n, T+1), starting with one share at column 0.

    Returns (final value, value of just holding, trades), each per path.
    """
    prices = np.atleast_2d(np.asarray(prices, dtype=float))
    n, t_len = prices.shape
    keep = 1.0 - commission_rate
    holding = np.ones(n, dtype=bool)
    shares, cash, trades = np.ones(n), np.zeros(n), np.zeros(n)
    upper = lower = prices[:, 0]
    for t in range(t_len):
        p = prices[:, t]
        if t % SWING_DAYS == 0:                       # new week: bands around today's close
            a = _band(np.asarray(sigmas)[t] if np.ndim(sigmas) else sigmas)
            upper, lower = p * math.e ** a, p * math.e ** -a
            continue
        sell = holding & (p >= upper)
        cash = np.where(sell, shares * upper * keep, cash)
        shares = np.where(sell, 0.0, shares)
        holding = holding & ~sell
        buy = ~holding & ~sell & (p <= lower)
        shares = np.where(buy, cash * keep / lower, shares)
        cash = np.where(buy, 0.0, cash)
        holding = holding | buy
        trades += sell.astype(float) + buy.astype(float)
    final = np.where(holding, shares * prices[:, -1], cash)
    return final, prices[:, -1], trades


def swing_analysis(closes: np.ndarray, month_paths_: np.ndarray, *, commission_rate: float) -> Dict[str, Any]:
    """This week's swing bands, the rule on simulated months, and on the stock's real last two years."""
    closes = np.asarray(closes, dtype=float)
    logret = np.diff(np.log(closes))
    if logret.size < SWING_LOOKBACK + 21:
        return {"status": "insufficient_history"}
    sigma = float(logret[-SWING_LOOKBACK:].std(ddof=1))
    p0 = float(closes[-1])
    a = float(_band(np.asarray(sigma)))
    sim_prices = np.column_stack([np.full(month_paths_.shape[0], p0), month_paths_])
    final, hold, trades = run_swing(sim_prices, sigma, commission_rate=commission_rate)
    sim_excess = (final / hold - 1) * 100

    # The real past: the rule applied to each of the last ~24 months, starting each month holding one share,
    # with band widths from the volatility known at each weekly reset.
    roll = pd.Series(logret).rolling(SWING_LOOKBACK).std(ddof=1).to_numpy()
    sig_at = np.concatenate([[np.nan], roll])                  # sigma known at the close of each day
    start = max(SWING_LOOKBACK + 1, closes.size - BACKTEST_DAYS - 1)
    months = []
    for s0 in range(start, closes.size - 21, 21):
        f, h, tr = run_swing(closes[s0:s0 + 22][None, :], sig_at[s0:s0 + 22], commission_rate=commission_rate)
        months.append((float(f[0] / h[0] - 1) * 100, float(tr[0])))
    n = len(months)
    wins = sum(1 for e, _ in months if e > 0)
    mean_excess = float(np.mean([e for e, _ in months])) if months else None
    low = wilson_low(wins / n, n) if n else 0.0
    if n < MIN_SWING_MONTHS:
        verdict = "too_little_history"
    elif mean_excess is not None and mean_excess > 0 and wins * 2 >= n:
        verdict = "proven" if low > 0.5 else "helped"
    else:
        verdict = "did_not_help"
    return {
        "status": "ok",
        "sell_at": p0 * math.exp(a), "buy_back_at": p0 * math.exp(-a), "band_pct": (math.exp(a) - 1) * 100,
        "sim_trades_per_month": float(trades.mean()), "sim_excess_pct": float(sim_excess.mean()),
        "sim_beats_hold": float(np.mean(sim_excess > 0)),
        "past_months": n, "past_wins": wins, "past_mean_excess_pct": mean_excess,
        "past_trades_per_month": float(np.mean([t for _, t in months])) if months else None,
        "verdict": verdict,
        **_two_years(closes, lambda seg, s0: run_swing(seg[None, :], sig_at[s0:s0 + len(seg)],
                                                         commission_rate=commission_rate)),
    }


def _two_years(closes: np.ndarray, rule) -> Dict[str, Any]:
    """The rule run continuously over the last two years against holding."""
    s0 = max(SWING_LOOKBACK + 1, closes.size - BACKTEST_DAYS - 1)
    seg = closes[s0:]
    if seg.size < 22:
        return {"past_2y_rule_pct": None, "past_2y_hold_pct": None, "past_2y_trades": None}
    final, hold, trades = rule(seg, s0)
    return {"past_2y_rule_pct": float(final[0] / seg[0] - 1) * 100, "past_2y_hold_pct": float(hold[0] / seg[0] - 1) * 100,
            "past_2y_trades": int(trades[0])}


def run_trend(prices: np.ndarray, sma: np.ndarray, *, commission_rate: float):
    """Hold while the close is at or above its moving average, stay out below it (one series, one share)."""
    keep = 1.0 - commission_rate
    shares, cash, inside, trades = 1.0, 0.0, True, 0
    for t in range(1, prices.size):
        p, m = prices[t], sma[t]
        if np.isnan(m):
            continue
        if inside and p < m:
            cash, shares, inside, trades = shares * p * keep, 0.0, False, trades + 1
        elif not inside and p > m:
            shares, cash, inside, trades = cash * keep / p, 0.0, True, trades + 1
    final = shares * prices[-1] if inside else cash
    return np.array([final]), np.array([prices[-1]]), np.array([trades])


def trend_analysis(closes: np.ndarray, *, commission_rate: float) -> Dict[str, Any]:
    """Trend following: out of the stock below its 20-day average, back in above it; judged on the real past."""
    closes = np.asarray(closes, dtype=float)
    if closes.size < SWING_LOOKBACK + 43:
        return {"status": "insufficient_history"}
    sma = pd.Series(closes).rolling(TREND_SMA).mean().to_numpy()
    start = max(SWING_LOOKBACK + 1, closes.size - BACKTEST_DAYS - 1)
    months = []
    for s0 in range(start, closes.size - 21, 21):
        f, h, tr = run_trend(closes[s0:s0 + 22], sma[s0:s0 + 22], commission_rate=commission_rate)
        months.append((float(f[0] / h[0] - 1) * 100, float(tr[0])))
    n = len(months)
    wins = sum(1 for e, _ in months if e > 0)
    mean_excess = float(np.mean([e for e, _ in months])) if months else None
    two = _two_years(closes, lambda seg, s0: run_trend(seg, sma[s0:s0 + len(seg)], commission_rate=commission_rate))
    beat = two["past_2y_rule_pct"] is not None and two["past_2y_rule_pct"] > two["past_2y_hold_pct"]
    if n < MIN_SWING_MONTHS:
        verdict = "too_little_history"
    elif beat and mean_excess is not None and mean_excess > 0:
        verdict = "proven" if wilson_low(wins / n, n) > 0.5 else "helped"
    else:
        verdict = "did_not_help"
    last, avg = float(closes[-1]), float(sma[-1])
    return {
        "status": "ok", "average": avg, "above_average": last >= avg,
        "past_months": n, "past_wins": wins, "past_mean_excess_pct": mean_excess,
        "past_trades_per_month": float(np.mean([t for _, t in months])) if months else None,
        "verdict": verdict, **two,
    }


def best_rule(rules: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The multi-trade rule to recommend: one that beat holding in the real last two years, the larger win first."""
    ok = [(name, r) for name, r in rules.items()
          if r.get("status") == "ok" and r.get("verdict") in ("proven", "helped")
          and r.get("past_2y_rule_pct") is not None
          and r["past_2y_rule_pct"] - r["past_2y_hold_pct"] >= MIN_LEAD_PP]
    if not ok:
        return None
    return max(ok, key=lambda kv: kv[1]["past_2y_rule_pct"] - kv[1]["past_2y_hold_pct"])[0]


def plan_paths(bars: pd.DataFrame, *, key: str, with_trend: bool, days: int = 21) -> Optional[np.ndarray]:
    """2,000 simulated futures over ``days``; with the shrunk trend only if it is validated."""
    closes = _closes(bars)
    if closes.size < MIN_DAYS:
        return None
    model = TrendBootstrap(_cfg(HORIZONS)) if with_trend else BootstrapForecaster(_cfg(HORIZONS))
    return model.paths(closes[-(model.window + 1):], days, key=key)


def month_paths(bars: pd.DataFrame, *, key: str, with_trend: bool) -> Optional[np.ndarray]:
    return plan_paths(bars, key=key, with_trend=with_trend, days=21)


def exit_plan(paths: np.ndarray, *, price: float, cost_per_share: float, shares: float,
              commission_rate: float, trend_used: bool, horizon_label: str = "a month") -> Dict[str, Any]:
    """The sell price with the best trade-off between price and the chance of reaching it in time."""
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
        "why": (f"even the best limit adds less than 0.5% over today's price within {horizon_label}"
                if sell_now else
                f"best trade-off between a higher price and the chance of reaching it within {horizon_label}"),
        "confidence": "uses a validated trend" if trend_used else "no proven trend: volatility only",
        "horizon_label": horizon_label,
    }


def prognosis(outlook: Optional[Dict[str, Any]], *, cost_per_share: float,
              commission_rate: float) -> Dict[str, Any]:
    """The main verdict for one lot: is waiting worth it, the expected price and date, the risk."""
    if not outlook or outlook.get("status") != "ok" or not cost_per_share:
        return {}
    hz = outlook["horizons"]
    keep = 1.0 - commission_rate
    p0 = outlook["last_close"]
    proven = [x for x in hz.values() if x.get("validated")]
    default = str(STYLES[outlook.get("style", "long")]["default"])
    main = max(proven, key=lambda x: x["confidence_low"]) if proven else hz[default]
    if proven and main["trend"] == "up":
        verdict, headline = "wait", "Waiting looks worthwhile: likely to rise"
    elif proven and main["trend"] == "down":
        verdict, headline = "fall", "Likely to fall: selling sooner loses less"
    else:
        verdict, headline = "unclear", "No reliable direction"
    break_even = cost_per_share / keep
    path = outlook.get("median_path") or []
    be_day = None
    if p0 < break_even:
        be_day = next((i + 1 for i, v in enumerate(path) if v >= break_even), None)
    as_of = pd.Timestamp(outlook["as_of"])
    return {
        "verdict": verdict, "headline": headline,
        "basis": (f"{main['label']} trend right {main['hits']} of {main['tests']} times"
                  if proven else "no trend passed its back-test"),
        "lean": None if proven else hz[default]["trend"],
        "horizon": main["label"], "expected": main["median"], "expected_date": main["date"],
        "expected_vs_cost_pct": (main["median"] * keep / cost_per_share - 1) * 100,
        "now_vs_cost_pct": (p0 * keep / cost_per_share - 1) * 100,
        "break_even": break_even,
        "in_profit": p0 >= break_even,
        "break_even_date": (as_of + pd.offsets.BDay(be_day)).strftime("%Y-%m-%d") if be_day else None,
        "p_halve_1y": outlook.get("p_halve_1y"),
    }
