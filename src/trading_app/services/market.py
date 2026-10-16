"""Price history and technical indicators for the dashboard charts.

Charts show the last year of daily bars. Twenty-one months are fetched so the
200-day average is defined across the whole displayed year rather than only
its last half. Prices are split- and dividend-adjusted, so a dividend is not
drawn as a drop.

Yahoo calls are spaced like the pipelines' (0.4 s) and cached on disk for six
hours, so opening the dashboard costs one request per stock at most a few
times a day. They go through their own throttle with short pauses: a page
should say "Yahoo is rate limiting, try again shortly" rather than hang for
the ten minutes a pipeline is willing to wait.

The indicators are **descriptive**: they summarise where a price has been
(trend, volatility, momentum, retracement levels), not where it will go. The
monitor's validated forecasting is the place for forward-looking claims.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from trading_common.rate_limiter import RateLimiter
from trading_common.throttle import ThrottlePolicy, get_throttle

logger = logging.getLogger(__name__)

DISPLAY_DAYS = 365
FETCH_DAYS = 700                        # 1 year + the 200 trading days the SMA200 needs
FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9\-]{0,11}(\.[A-Z]{2,3})?$")
BENCHMARK = "^GSPC"                     # S&P 500, the yardstick in the sector view
_PACER = RateLimiter(0.4)              # same spacing as the pipelines' yfinance calls
# One Yahoo request at a time from the app: the page's charts, live prices and
# forecasts run in parallel threads, and overlapping yfinance calls both burst
# past Yahoo's limits and share one cookie/crumb session.
_YF_LOCK = threading.Lock()
QUIET_SECONDS = 300                    # after Yahoo goes silent, use last known prices this long
EMPTY_RUN = 3                          # this many empty answers in a row = Yahoo is not answering
# yfinance logs "possibly delisted; no price data found" whenever Yahoo answers
# empty -- which is also what a block looks like. The app reports that itself,
# once and correctly, so the misleading per-ticker line is silenced.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
THROTTLE = "yahoo_charts"
QUOTE_TTL = 300                         # live prices refresh at most every 5 minutes
CHART_POLICY = ThrottlePolicy(base_pause=3.0, max_pause=10.0, give_up_after=3, cooldown=120.0)


class MarketDataError(RuntimeError):
    pass


def normalize_symbol(raw: str) -> str:
    """A Yahoo symbol: ``brk.b`` -> ``BRK-B``, ``walmex.mx`` -> ``WALMEX.MX``, ``^gspc``.

    A one-letter suffix is a share class (Yahoo writes it with a hyphen); two
    or three letters are an exchange (``.MX`` Mexico, ``.TO`` Toronto).
    """
    s = re.sub(r"[\s*]", "", str(raw or "")).upper()
    s = re.sub(r"\.([A-Z])$", r"-\1", s)
    if not SYMBOL_RE.match(s):
        raise ValueError(f"{str(raw)[:20]!r} is not a ticker symbol (examples: AAPL, BRK.B, WALMEX.MX)")
    return s


# ---------------------------------------------------------------------------
# Fetching (cached, throttled)
# ---------------------------------------------------------------------------

class MarketData:
    def __init__(self, cache_dir: Path, *, ttl_seconds: int = 6 * 3600, offline: bool = False,
                 downloader=None, info_downloader=None, quote_downloader=None,
                 quote_ttl_seconds: int = QUOTE_TTL) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl = ttl_seconds
        self.quote_ttl = quote_ttl_seconds
        self.offline = offline
        self._download = downloader or _yahoo_history
        self._info = info_downloader or _yahoo_info
        self._quote = quote_downloader or _yahoo_quote
        self.quiet_until = 0.0              # Yahoo stopped answering: no quote requests before this time

    def bars(self, symbol: str, start: Optional[date] = None) -> pd.DataFrame:
        """Daily bars from ``start`` (default: enough for a year plus the SMA200 warm-up)."""
        symbol = normalize_symbol(symbol)
        want = min(date.today() - timedelta(days=FETCH_DAYS), start or date.max)
        path = self.cache_dir / "prices" / f"{symbol}.json"
        cached = _read_cache(path, self.ttl if not self.offline else None)
        # A cache from before "requested_from" existed counts only from the first date it holds.
        reach = (cached or {}).get("requested_from") or ((cached or {}).get("dates") or ["9999-12-31"])[0]
        covers = cached is not None and date.fromisoformat(reach) <= want
        if cached is not None and (covers or self.offline):
            return _frame(cached)
        if self.offline:
            raise MarketDataError(f"no cached prices for {symbol} (offline)")
        try:
            df = get_throttle(THROTTLE, CHART_POLICY).call(self._download, symbol, want)
        except Exception as exc:  # noqa: BLE001 - surfaced to the page as a message
            raise MarketDataError(f"{symbol}: {type(exc).__name__}: {exc}") from None
        if df is None or df.empty:
            raise MarketDataError(f"{symbol}: Yahoo returned no prices (check the ticker)")
        _write_cache(path, {**_payload(df), "requested_from": want.isoformat()})
        return df

    def quotes(self, symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Latest price per symbol, refreshed at most every ``quote_ttl`` seconds.

        One small request per stale symbol (the last five days of 5-minute
        bars), spaced like every other Yahoo call. A symbol Yahoo cannot price
        comes back with an ``error`` instead of a number.
        """
        path = self.cache_dir / "quotes.json"
        try:
            book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            book = {}
        now = time.time()
        out: Dict[str, Dict[str, Any]] = {}
        changed = False
        empty_run = 0
        for raw in symbols:
            symbol = normalize_symbol(raw)
            hit = book.get(symbol)
            if hit and (self.offline or now - hit.get("fetched", 0) < self.quote_ttl):
                out[symbol] = hit
                continue
            if self.offline:
                out[symbol] = {"error": "offline: no quote cached"}
                continue
            if time.time() < self.quiet_until:
                # Yahoo stopped answering moments ago: keep the last known price, ask again later.
                out[symbol] = {**(hit or {}), "error": "Yahoo is not answering (rate limit); last known price",
                               "stale": True}
                continue
            try:
                q = get_throttle(THROTTLE, CHART_POLICY).call(self._quote, symbol)
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not blank the table
                out[symbol] = {**(hit or {}), "error": f"{type(exc).__name__}: {exc}"[:160], "stale": True}
                continue
            if not q or q.get("price") is None:
                out[symbol] = {**(hit or {}), "error": "Yahoo returned no recent price", "stale": True}
                empty_run += 1
                if empty_run >= EMPTY_RUN:
                    self.quiet_until = time.time() + QUIET_SECONDS
                    logger.warning("Yahoo returned no prices for %d symbols in a row (it does this when it rate "
                                   "limits); using last known prices and asking again in %d s",
                                   empty_run, QUIET_SECONDS)
                continue
            empty_run = 0
            book[symbol] = out[symbol] = {**q, "fetched": now}
            changed = True
        if changed:
            _write_cache(path, book)
        return out

    def cached_quotes(self, symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Whatever quotes are on disk, however old -- never touches the network."""
        path = self.cache_dir / "quotes.json"
        try:
            book = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            return {}
        return {normalize_symbol(s): book[normalize_symbol(s)] for s in symbols if normalize_symbol(s) in book}

    def cached_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        """The bars on disk, however old -- never touches the network (None if never fetched)."""
        cached = _read_cache(self.cache_dir / "prices" / f"{normalize_symbol(symbol)}.json", None)
        return _frame(cached) if cached is not None else None

    def cached_meta(self, symbol: str) -> Dict[str, Optional[str]]:
        """Sector and name if already looked up -- never touches the network."""
        return _read_cache(self.cache_dir / "meta" / f"{normalize_symbol(symbol)}.json", None) or {}

    def meta(self, symbol: str, known: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Optional[str]]:
        """Sector and name: from the ranking when it has the stock, else Yahoo (cached 30 days)."""
        symbol = normalize_symbol(symbol)
        hit = (known or {}).get(symbol)
        if hit and hit.get("sector"):
            return {"sector": hit["sector"], "name": hit.get("name")}
        path = self.cache_dir / "meta" / f"{symbol}.json"
        cached = _read_cache(path, 30 * 86400 if not self.offline else None)
        if cached is not None:
            return cached
        if self.offline:
            return {"sector": None, "name": None}
        try:
            info = get_throttle(THROTTLE, CHART_POLICY).call(self._info, symbol) or {}
        except Exception as exc:  # noqa: BLE001 - a missing sector is not fatal
            logger.info("sector lookup failed for %s: %s", symbol, exc)
            return {"sector": None, "name": None}
        kind = str(info.get("quoteType") or "").upper()
        sector = info.get("sector") or {"ETF": "ETF / Fund", "MUTUALFUND": "ETF / Fund",
                                        "INDEX": "Index"}.get(kind)
        meta = {"sector": sector, "name": info.get("shortName") or info.get("longName")}
        _write_cache(path, meta)
        return meta


def _yahoo_history(symbol: str, start: date) -> pd.DataFrame:
    import yfinance as yf

    with _YF_LOCK:
        _PACER.acquire()
        df = yf.Ticker(symbol).history(start=start.isoformat(), auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    idx = pd.DatetimeIndex(df.index)
    df.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    df = df.dropna(subset=["close"])
    try:
        with _YF_LOCK:
            df.attrs["currency"] = (yf.Ticker(symbol).history_metadata or {}).get("currency")
    except Exception:  # noqa: BLE001 - the currency label is cosmetic
        pass
    return df


def _yahoo_quote(symbol: str) -> Dict[str, Any]:
    """Last regular-session 5-minute close and the previous session's close."""
    import yfinance as yf

    with _YF_LOCK:
        _PACER.acquire()
        df = yf.Ticker(symbol).history(period="5d", interval="5m", prepost=False, auto_adjust=False)
    if df is None or df.empty:
        return {}
    close = df["Close"].dropna()
    if close.empty:
        return {}
    last_t = close.index[-1]
    sessions = pd.Series(close.index.date, index=close.index)
    earlier = close[sessions < last_t.date()]
    prev = float(earlier.iloc[-1]) if len(earlier) else None
    price = float(close.iloc[-1])
    return {"price": price, "prev_close": prev, "as_of": last_t.isoformat(),
            "change_pct": (price / prev - 1) * 100 if prev else None}


def _yahoo_info(symbol: str) -> Dict[str, Any]:
    import yfinance as yf

    with _YF_LOCK:
        _PACER.acquire()
        return dict(yf.Ticker(symbol).get_info() or {})


def _payload(df: pd.DataFrame) -> Dict[str, Any]:
    return {"dates": [d.strftime("%Y-%m-%d") for d in df.index], "currency": df.attrs.get("currency"),
            **{c: [None if pd.isna(v) else float(v) for v in df[c]] for c in ("open", "high", "low", "close", "volume")}}


def _frame(p: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame({c: p[c] for c in ("open", "high", "low", "close", "volume")},
                      index=pd.to_datetime(p["dates"])).astype(float)
    df.attrs["currency"] = p.get("currency")
    return df


def _read_cache(path: Path, ttl: Optional[int]) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    if ttl is not None and time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Indicators (pure functions on a daily OHLCV frame)
# ---------------------------------------------------------------------------

def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def fibonacci(window: pd.DataFrame) -> Dict[str, Any]:
    """Retracement levels between the window's swing low and swing high.

    In an advance (the low came first) levels are measured down from the
    high; in a decline, up from the low -- the conventional reading.
    """
    hi_t, lo_t = window["high"].idxmax(), window["low"].idxmin()
    hi, lo = float(window["high"].max()), float(window["low"].min())
    up = lo_t < hi_t
    span = hi - lo
    levels = [{"ratio": r, "price": (hi - span * r) if up else (lo + span * r)} for r in FIB_RATIOS]
    last = float(window["close"].iloc[-1])
    ordered = sorted(levels, key=lambda lv: lv["price"])
    zone = None
    for below, above in zip(ordered, ordered[1:]):
        if below["price"] <= last <= above["price"]:
            zone = f"{100 * min(below['ratio'], above['ratio']):.1f}%–{100 * max(below['ratio'], above['ratio']):.1f}%"
            break
    return {"direction": "up" if up else "down", "high": hi, "low": lo,
            "high_date": hi_t.strftime("%Y-%m-%d"), "low_date": lo_t.strftime("%Y-%m-%d"),
            "levels": levels, "zone": zone}


def stats(df: pd.DataFrame, window: pd.DataFrame) -> Dict[str, Any]:
    close = window["close"]
    last = float(close.iloc[-1])
    rets = np.log(close).diff().dropna()
    peak = close.cummax()
    s50, s200 = sma(df["close"], 50), sma(df["close"], 200)
    r = rsi(df["close"])
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=14).mean()

    def last_of(s):
        v = s.iloc[-1] if len(s) else np.nan
        return None if pd.isna(v) else float(v)

    sma50, sma200 = last_of(s50), last_of(s200)
    return {
        "last": last,
        "change_1d_pct": _pct(last, float(close.iloc[-2])) if len(close) > 1 else None,
        "return_1y_pct": _pct(last, float(close.iloc[0])),
        "volatility_pct": float(rets.std(ddof=1) * math.sqrt(252) * 100) if len(rets) > 2 else None,
        "max_drawdown_pct": float((close / peak - 1).min() * 100),
        "high_52w": float(window["high"].max()),
        "low_52w": float(window["low"].min()),
        "from_high_pct": _pct(last, float(window["high"].max())),
        "rsi14": last_of(r),
        "sma50": sma50,
        "sma200": sma200,
        "vs_sma200_pct": _pct(last, sma200) if sma200 else None,
        "trend": (None if sma50 is None or sma200 is None else
                  "up" if (last > sma200 and sma50 > sma200) else
                  "down" if (last < sma200 and sma50 < sma200) else "mixed"),
        "atr14_pct": (last_of(atr) / last * 100) if last_of(atr) else None,
    }


def _pct(a: float, b: Optional[float]) -> Optional[float]:
    return None if not b else (a / b - 1.0) * 100.0


def _series(s: pd.Series, index: Iterable) -> List[Dict[str, Any]]:
    s = s.reindex(index)
    return [{"time": t.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
            for t, v in s.items() if not pd.isna(v)]


def clean(obj: Any) -> Any:
    """NaN/inf -> None, numpy scalars -> Python, recursively (JSON cannot carry NaN)."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (bool, np.bool_)):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(obj) else None
    return obj


def chart_payload(symbol: str, df: pd.DataFrame, *, days: int = DISPLAY_DAYS) -> Dict[str, Any]:
    """Everything one stock chart needs, ready for the page."""
    cutoff = df.index[-1] - pd.Timedelta(days=days)
    window = df.loc[df.index > cutoff]
    if len(window) < 20:
        raise MarketDataError(f"{symbol}: not enough price history to chart")
    idx = window.index
    up, mid, low = bollinger(df["close"])
    bars = [{"time": t.strftime("%Y-%m-%d"), "open": round(r.open, 4), "high": round(r.high, 4),
             "low": round(r.low, 4), "close": round(r.close, 4)} for t, r in window.iterrows()]
    volume = [{"time": t.strftime("%Y-%m-%d"), "value": 0.0 if pd.isna(r.volume) else float(r.volume),
               "up": bool(r.close >= r.open)} for t, r in window.iterrows()]
    return {
        "symbol": symbol,
        "currency": df.attrs.get("currency"),
        "as_of": idx[-1].strftime("%Y-%m-%d"),
        "bars": bars,
        "volume": volume,
        "sma50": _series(sma(df["close"], 50), idx),
        "sma200": _series(sma(df["close"], 200), idx),
        "bb_upper": _series(up, idx), "bb_mid": _series(mid, idx), "bb_lower": _series(low, idx),
        "rsi": _series(rsi(df["close"]), idx),
        "fibonacci": fibonacci(window),
        # Statistics always describe the last year, however far back the chart reaches.
        "stats": stats(df, df.loc[df.index > df.index[-1] - pd.Timedelta(days=DISPLAY_DAYS)]),
    }


def _rebased(df: pd.DataFrame, days: int) -> Optional[pd.Series]:
    cutoff = df.index[-1] - pd.Timedelta(days=days)
    w = df.loc[df.index > cutoff, "close"]
    return (w / w.iloc[0] - 1.0) * 100.0 if len(w) >= 20 else None


def sector_payload(name: str, frames: Dict[str, pd.DataFrame], *, benchmark: Optional[pd.DataFrame] = None,
                   days: int = DISPLAY_DAYS) -> Dict[str, Any]:
    """Members rebased to 0% at the window start, an equal-weight sector index,
    and the benchmark (S&P 500) on the same footing for relative strength."""
    rebased = {sym: r for sym, r in ((sym, _rebased(df, days)) for sym, df in frames.items()) if r is not None}
    if not rebased:
        raise MarketDataError(f"{name}: no member has enough history")
    table = pd.DataFrame(rebased).sort_index().ffill()
    index = ((1 + table / 100).mean(axis=1) - 1) * 100
    level = 100 * (1 + index / 100)
    rets = np.log(level).diff().dropna()
    bench = _rebased(benchmark, days) if benchmark is not None and len(benchmark) else None
    bench_1y = float(bench.iloc[-1]) if bench is not None else None
    members = {}
    for sym, df in frames.items():
        if sym in rebased:
            cutoff = df.index[-1] - pd.Timedelta(days=days)
            st = stats(df, df.loc[df.index > cutoff])
            st["vs_benchmark_pct"] = None if bench_1y is None else st["return_1y_pct"] - bench_1y
            members[sym] = st
    return {
        "sector": name,
        "series": {sym: _series(table[sym], table.index) for sym in table.columns},
        "index": _series(index, table.index),
        "index_sma50": _series(sma(index, 50), table.index),
        "benchmark": None if bench is None else _series(bench, bench.index),
        "stats": {
            "return_1y_pct": float(index.iloc[-1]),
            "benchmark_1y_pct": bench_1y,
            "vs_benchmark_pct": None if bench_1y is None else float(index.iloc[-1]) - bench_1y,
            "volatility_pct": float(rets.std(ddof=1) * math.sqrt(252) * 100) if len(rets) > 2 else None,
            "max_drawdown_pct": float((level / level.cummax() - 1).min() * 100),
            "members": len(rebased),
        },
        "members": members,
    }


def generated_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
