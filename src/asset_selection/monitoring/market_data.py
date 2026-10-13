"""Market data for the monitor: daily history, USDMXN, and earnings events.

The selection pipeline's price provider returns a summary snapshot; the monitor
needs the full daily series (trend, drawdown since purchase, forecasting
context, walk-forward validation), so it has its own small history layer.

Contract (same as the selection providers):

* A transport failure is **reported**, never swallowed into "no data": the
  returned object carries ``error`` and a classified ``error_type``.
* Offline mode reads the cache regardless of age and labels the result
  ``cache_stale``; it never touches the network and never invents a series.
* ``StaticMarketData`` serves injected frames, for tests and for
  user-supplied prices.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading_common.cache import Cache
from trading_common.errors import classify_exception
from trading_common.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

FX_SYMBOL = "MXN=X"          # Yahoo quotes USDMXN as MXN per 1 USD


@dataclass
class PriceHistory:
    symbol: str
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)   # index: naive dates; close, adj_close
    source: str = "none"     # live | cache | cache_stale | static | none
    error: Optional[str] = None
    error_type: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.frame is not None and not self.frame.empty

    @property
    def last_date(self) -> Optional[date]:
        return self.frame.index[-1].date() if self.ok else None

    @property
    def last_close(self) -> Optional[float]:
        return float(self.frame["close"].iloc[-1]) if self.ok else None

    def close_on_or_before(self, when: date) -> Optional[float]:
        """Raw (unadjusted) close on ``when`` or the last trading day before it."""
        if not self.ok:
            return None
        s = self.frame["close"].loc[: pd.Timestamp(when)]
        return float(s.iloc[-1]) if len(s) else None


@dataclass
class EarningsInfo:
    next_date: Optional[date] = None
    # (report date, EPS estimate, reported EPS, surprise %), newest first.
    history: List[Tuple[date, Optional[float], Optional[float], Optional[float]]] = field(
        default_factory=list
    )
    source: str = "none"
    error: Optional[str] = None


class MarketData(ABC):
    @abstractmethod
    def history(self, symbol: str, start: date) -> PriceHistory: ...

    @abstractmethod
    def earnings(self, symbol: str) -> EarningsInfo: ...

    def fx_usdmxn(self, start: date) -> PriceHistory:
        return self.history(FX_SYMBOL, start)


# ---------------------------------------------------------------------------
# yfinance-backed implementation
# ---------------------------------------------------------------------------

class YFinanceMarketData(MarketData):
    HISTORY_NS = "monitor_history"
    EARNINGS_NS = "monitor_earnings"

    def __init__(
        self,
        cache: Optional[Cache] = None,
        rate_limiter: Optional[RateLimiter] = None,
        *,
        ttl_seconds: int = 43_200,
        offline: bool = False,
    ) -> None:
        self.cache = cache or Cache(enabled=False)
        self.rate_limiter = rate_limiter or RateLimiter(0.0)
        self.ttl = int(ttl_seconds)
        self.offline = offline

    # ------------------------------------------------------------------
    def history(self, symbol: str, start: date) -> PriceHistory:
        key = f"{symbol}:{start.isoformat()}"
        cached = self.cache.get(self.HISTORY_NS, key, ttl=self.ttl)
        if cached is not None:
            return PriceHistory(symbol, _frame_from_payload(cached), source="cache")
        if self.offline:
            entry = self.cache.get_entry(self.HISTORY_NS, key)
            if entry is not None:
                return PriceHistory(symbol, _frame_from_payload(entry.payload), source="cache_stale")
            return PriceHistory(symbol, source="none", error="offline and no cached history",
                                error_type="OFFLINE_NO_CACHE")
        try:
            frame = self._download(symbol, start)
        except Exception as exc:  # noqa: BLE001 - classified and reported
            logger.warning("history download failed for %s: %s", symbol, exc)
            return PriceHistory(symbol, source="none", error=f"{type(exc).__name__}: {exc}",
                                error_type=classify_exception(exc))
        if frame.empty:
            return PriceHistory(symbol, source="none", error="provider returned no rows",
                                error_type="NO_PRICE_DATA")
        self.cache.set(self.HISTORY_NS, key, _frame_to_payload(frame))
        return PriceHistory(symbol, frame, source="live")

    def _download(self, symbol: str, start: date) -> pd.DataFrame:
        import yfinance as yf

        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            try:
                self.rate_limiter.acquire()
                df = yf.Ticker(symbol).history(start=start.isoformat(), auto_adjust=False)
                return _normalize_frame(df)
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    def earnings(self, symbol: str) -> EarningsInfo:
        cached = self.cache.get(self.EARNINGS_NS, symbol, ttl=self.ttl)
        if cached is None and self.offline:
            entry = self.cache.get_entry(self.EARNINGS_NS, symbol)
            cached = entry.payload if entry is not None else None
            if cached is None:
                return EarningsInfo(source="none", error="offline and no cached earnings")
        if cached is not None:
            return _earnings_from_payload(cached, source="cache")
        try:
            info = self._download_earnings(symbol)
        except Exception as exc:  # noqa: BLE001 - events are optional context
            logger.info("earnings lookup failed for %s: %s", symbol, exc)
            return EarningsInfo(source="none", error=f"{type(exc).__name__}: {exc}")
        self.cache.set(self.EARNINGS_NS, symbol, _earnings_to_payload(info))
        return info

    def _download_earnings(self, symbol: str) -> EarningsInfo:
        import yfinance as yf

        self.rate_limiter.acquire()
        tk = yf.Ticker(symbol)
        info = EarningsInfo(source="live")
        today = date.today()
        try:
            cal = tk.calendar
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            upcoming = sorted(d for d in (dates or []) if isinstance(d, date) and d >= today)
            info.next_date = upcoming[0] if upcoming else None
        except Exception as exc:  # noqa: BLE001 - partial info is still useful
            info.error = f"calendar: {type(exc).__name__}: {exc}"
        try:
            ed = tk.get_earnings_dates(limit=12)
        except Exception as exc:  # noqa: BLE001
            ed = None
            info.error = (info.error + "; " if info.error else "") + f"earnings_dates: {exc}"
        if ed is not None and not ed.empty:
            for ts, row in ed.iterrows():
                d = pd.Timestamp(ts).date()
                est, rep, sur = (_num(row.get(c)) for c in ("EPS Estimate", "Reported EPS", "Surprise(%)"))
                if rep is None:
                    if d >= today and (info.next_date is None or d < info.next_date):
                        info.next_date = d
                    continue
                info.history.append((d, est, rep, sur))
            info.history.sort(key=lambda r: r[0], reverse=True)
        return info


# ---------------------------------------------------------------------------
# Static implementation (tests, user-supplied data)
# ---------------------------------------------------------------------------

class StaticMarketData(MarketData):
    """Serves injected series. Missing symbols are reported as missing."""

    def __init__(
        self,
        histories: Optional[Dict[str, pd.DataFrame]] = None,
        earnings: Optional[Dict[str, EarningsInfo]] = None,
    ) -> None:
        self.histories = {k: _normalize_frame(v) for k, v in (histories or {}).items()}
        self._earnings = earnings or {}

    def history(self, symbol: str, start: date) -> PriceHistory:
        frame = self.histories.get(symbol)
        if frame is None or frame.empty:
            return PriceHistory(symbol, source="none", error="no data supplied",
                                error_type="NO_PRICE_DATA")
        return PriceHistory(symbol, frame.loc[pd.Timestamp(start):], source="static")

    def earnings(self, symbol: str) -> EarningsInfo:
        return self._earnings.get(symbol, EarningsInfo(source="none"))


class OverlayMarketData(MarketData):
    """Wrap a provider and pin the latest close of chosen symbols to user prices.

    Used for ``--price TICKER=VALUE``: the history still comes from the
    provider (so trend and forecasting work), but today's close is the user's
    value -- appended as a new row dated today, or replacing today's row.
    """

    def __init__(self, base: MarketData, overrides: Dict[str, float]) -> None:
        self.base = base
        self.overrides = {k.upper(): float(v) for k, v in overrides.items()}

    def history(self, symbol: str, start: date) -> PriceHistory:
        h = self.base.history(symbol, start)
        price = self.overrides.get(symbol.upper())
        if price is None:
            return h
        today = pd.Timestamp(date.today())
        row = pd.DataFrame({"close": [price], "adj_close": [price]}, index=[today])
        frame = row if not h.ok else pd.concat([h.frame.loc[: today - pd.Timedelta(days=1)], row])
        return PriceHistory(symbol, frame, source=f"{h.source}+user_override", error=h.error,
                            error_type=None)

    def earnings(self, symbol: str) -> EarningsInfo:
        return self.base.earnings(symbol)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_frame(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Any OHLC-ish frame -> naive daily index with float close / adj_close."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["close", "adj_close"])
    cols = {c.lower().replace(" ", "_"): c for c in df.columns}
    close_col = cols.get("close")
    if close_col is None:
        return pd.DataFrame(columns=["close", "adj_close"])
    adj_col = cols.get("adj_close", close_col)
    out = pd.DataFrame({
        "close": pd.to_numeric(df[close_col], errors="coerce"),
        "adj_close": pd.to_numeric(df[adj_col], errors="coerce"),
    })
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    out["adj_close"] = out["adj_close"].fillna(out["close"])
    return out


def _frame_to_payload(frame: pd.DataFrame) -> Dict[str, Any]:
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in frame.index],
        "close": [float(x) for x in frame["close"]],
        "adj_close": [float(x) for x in frame["adj_close"]],
    }


def _frame_from_payload(payload: Dict[str, Any]) -> pd.DataFrame:
    idx = pd.to_datetime(payload.get("dates") or [])
    return pd.DataFrame(
        {"close": payload.get("close") or [], "adj_close": payload.get("adj_close") or []},
        index=idx,
    ).astype(float)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


def _earnings_to_payload(info: EarningsInfo) -> Dict[str, Any]:
    return {
        "next_date": info.next_date.isoformat() if info.next_date else None,
        "history": [[d.isoformat(), e, r, s] for d, e, r, s in info.history],
        "error": info.error,
    }


def _earnings_from_payload(payload: Dict[str, Any], *, source: str) -> EarningsInfo:
    nd = payload.get("next_date")
    nxt = datetime.strptime(nd, "%Y-%m-%d").date() if nd else None
    if nxt is not None and nxt < date.today():
        nxt = None                       # a cached "next" date that has since passed
    hist = []
    for d, e, r, s in payload.get("history") or []:
        hist.append((datetime.strptime(d, "%Y-%m-%d").date(), e, r, s))
    return EarningsInfo(next_date=nxt, history=hist, source=source, error=payload.get("error"))


def history_start(history_years: float, *, today: Optional[date] = None) -> date:
    t = today or date.today()
    return t - timedelta(days=int(round(365.25 * float(history_years))))
