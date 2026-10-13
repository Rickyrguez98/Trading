"""Evidence about each holding: price trend, scheduled events, selection view.

All trend measures use the **adjusted** close so dividends and splits do not
masquerade as price moves. Nothing here decides anything; :mod:`.decision`
weighs these signals and records how each one contributed.
"""
from __future__ import annotations

import ast
import json
import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

import numpy as np
import pandas as pd

from .config import EventsConfig, TrendConfig
from .market_data import EarningsInfo, PriceHistory
from .models import EventSignals, SelectionSignals, TrendSignals

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
_RETURN_WINDOWS = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------

def compute_trend(prices: PriceHistory, cfg: TrendConfig) -> TrendSignals:
    t = TrendSignals()
    if not prices.ok:
        return t
    s = prices.frame["adj_close"].astype(float)
    n = len(s)
    t.history_days = n
    last = float(s.iloc[-1])

    for label, w in _RETURN_WINDOWS.items():
        t.returns_pct[label] = _ret(s, w)
    if n > TRADING_DAYS:
        # Classic 12-1 momentum: the year's return excluding the latest month,
        # which tends to reverse rather than persist.
        t.momentum_12_1_pct = 100.0 * (float(s.iloc[-22]) / float(s.iloc[-TRADING_DAYS - 1]) - 1.0)

    window = s.iloc[-TRADING_DAYS:]
    t.high_52w, t.low_52w = float(window.max()), float(window.min())
    t.pct_from_52w_high = 100.0 * (last / t.high_52w - 1.0) if t.high_52w > 0 else None
    running_peak = window.cummax()
    t.max_drawdown_1y_pct = 100.0 * float((window / running_peak - 1.0).min())

    logret = np.log(s).diff().dropna()
    if len(logret) >= cfg.volatility_window:
        rolling = logret.rolling(cfg.volatility_window).std() * math.sqrt(TRADING_DAYS)
        current = float(rolling.iloc[-1])
        t.volatility_ann_pct = 100.0 * current
        hist = rolling.iloc[-cfg.volatility_regime_lookback:].dropna()
        med = float(hist.median()) if len(hist) else float("nan")
        if med and med == med and med > 0:
            t.volatility_regime_ratio = current / med

    if n < cfg.sma_long:
        return t                                  # INSUFFICIENT: no long average yet
    t.sma_short = float(s.iloc[-cfg.sma_short:].mean())
    t.sma_long = float(s.iloc[-cfg.sma_long:].mean())
    t.price_vs_sma_long_pct = 100.0 * (last / t.sma_long - 1.0)
    t.sma_cross = "golden" if t.sma_short > t.sma_long else "death"
    above = last > t.sma_long
    if above and t.sma_cross == "golden":
        t.trend_state = "UP"
    elif not above and t.sma_cross == "death":
        t.trend_state = "DOWN"
    else:
        t.trend_state = "MIXED"
    return t


def _ret(s: pd.Series, w: int) -> Optional[float]:
    if len(s) <= w:
        return None
    past = float(s.iloc[-w - 1])
    return 100.0 * (float(s.iloc[-1]) / past - 1.0) if past > 0 else None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def compute_events(info: EarningsInfo, cfg: EventsConfig, *, today: date) -> EventSignals:
    e = EventSignals(error=info.error)
    if info.next_date is not None:
        e.next_earnings_date = info.next_date.isoformat()
        e.days_to_earnings = (info.next_date - today).days
        e.in_earnings_blackout = 0 <= e.days_to_earnings <= cfg.earnings_blackout_days
    recent = [r for r in info.history if r[2] is not None][: cfg.surprise_history]
    if recent:
        d, est, rep, sur = recent[0]
        e.last_report_date = d.isoformat()
        e.last_surprise_pct = sur
        counted = [r for r in recent if r[1] is not None and r[2] is not None]
        e.reports_counted = len(counted)
        e.beats = sum(1 for r in counted if r[2] > r[1])
    return e


# ---------------------------------------------------------------------------
# Selection view
# ---------------------------------------------------------------------------

def load_selection_signals(
    csv_path: Union[str, Path, None],
    *,
    source: str = "full_ranking",
    run_id: Optional[str] = None,
) -> Dict[str, SelectionSignals]:
    """Read a selection results CSV into per-ticker signals (empty if absent)."""
    if not csv_path:
        return {}
    p = Path(csv_path)
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p)
    except Exception as exc:  # noqa: BLE001 - a broken file is reported, not fatal
        logger.warning("could not read selection results %s: %s", p, exc)
        return {}
    out: Dict[str, SelectionSignals] = {}
    for _, row in df.iterrows():
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        sentiment = _f(row.get("effective_sentiment_score"))
        if sentiment is None:
            sentiment = _f(row.get("sentiment_score"))
        out[ticker] = SelectionSignals(
            source=source,
            run_id=run_id,
            in_ranking=True,
            company_name=_str(row.get("company_name")),
            sector=_str(row.get("sector")),
            selection_rank=_i(row.get("rank")),
            fundamentals_score=_f(row.get("fundamentals_score")),
            final_score=_f(row.get("final_score")),
            effective_sentiment_score=sentiment,
            sentiment_confidence=_f(row.get("effective_sentiment_confidence"))
            if _f(row.get("effective_sentiment_confidence")) is not None
            else _f(row.get("sentiment_confidence")),
            article_count=_i(row.get("article_count")),
            risk_penalty=_f(row.get("risk_penalty")),
            candidate_role=_str(row.get("candidate_role")),
            eligible_for_allocation=_b(row.get("eligible_for_allocation")),
            flags=_list(row.get("flags")),
            exclusion_reasons=_list(row.get("allocation_exclusion_reasons")),
            data_quality_bucket=_str(row.get("data_quality_bucket")),
            missing_metric_count=_i(row.get("missing_metric_count")),
            is_etf=_b(row.get("is_etf")),
        )
    return out


# Sentiment is scored per stock on an absolute 0-100 scale, so a refresh on a
# handful of tickers is comparable to the full ranking. Fundamentals are NOT:
# they are z-scores across whatever set was scored together (see
# fundamentals/fundamental_scoring.py), so a 12-stock rescore would measure
# each holding against the other eleven, not against the market.
REFRESHABLE_FIELDS = ("effective_sentiment_score", "sentiment_confidence", "article_count")


def merge_refreshed_sentiment(
    base: Dict[str, SelectionSignals], refreshed: Dict[str, SelectionSignals]
) -> Dict[str, SelectionSignals]:
    """Thesis fields from the full ranking; only absolute sentiment from the refresh."""
    out = dict(base)
    for ticker, fresh in refreshed.items():
        if fresh.effective_sentiment_score is None:
            continue
        sig = out.get(ticker)
        if sig is None:
            # Not in the full ranking: no thesis, but the fresh news still informs.
            sig = SelectionSignals(source="full_ranking" if base else None,
                                   in_ranking=False, company_name=fresh.company_name)
            out[ticker] = sig
        for f in REFRESHABLE_FIELDS:
            setattr(sig, f, getattr(fresh, f))
        sig.sentiment_source = "refreshed"
    return out


def read_run_id(summary_json: Union[str, Path, None]) -> Optional[str]:
    if not summary_json:
        return None
    p = Path(summary_json)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("run_id") or data.get("generated_at")


def selection_timestamp(results_csv: Union[str, Path, None],
                        summary_json: Union[str, Path, None]) -> Optional[datetime]:
    """When the ranking was produced: the paired summary's generated_at, else the file's mtime."""
    if summary_json:
        p = Path(summary_json)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8")).get("generated_at")
                if raw:
                    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    if results_csv and Path(results_csv).exists():
        return datetime.fromtimestamp(Path(results_csv).stat().st_mtime, tz=timezone.utc)
    return None


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return None if f is None else int(f)


def _str(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v).strip()
    return s or None


def _b(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    s = _str(v)
    if s is None:
        return None
    if s.lower() in ("true", "1", "yes", "y"):
        return True
    if s.lower() in ("false", "0", "no", "n"):
        return False
    return None


def _list(v: Any) -> list:
    s = _str(v)
    if not s:
        return []
    try:
        parsed = ast.literal_eval(s)
        if isinstance(parsed, (list, tuple)):
            return [str(x) for x in parsed]
    except (ValueError, SyntaxError):
        pass
    return [x.strip() for x in s.replace(";", ",").split(",") if x.strip()]
