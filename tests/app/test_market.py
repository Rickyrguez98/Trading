"""Dashboard charts: indicators, caching, the watchlist and the market API.

Prices come from a fake downloader, so nothing here touches the network; the
numbers are checked against hand-computable series.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_app.services import InvalidRequest, Workflow, default_paths
from trading_app.services.jobs import JobManager, selection_progress
from trading_app.services.market import (
    CHART_POLICY,
    MarketData,
    MarketDataError,
    bollinger,
    chart_payload,
    clean,
    fibonacci,
    normalize_symbol,
    rsi,
    sector_payload,
    sma,
    stats,
)

REPO = Path(__file__).resolve().parents[2]


def _bars(closes, end="2026-10-14"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    df = pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
                       "volume": 1_000_000.0}, index=idx)
    df.attrs["currency"] = "USD"
    return df


def _trend(n=480, start=100.0, step=0.002):
    return _bars(start * (1 + step) ** np.arange(n))


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("aapl", "AAPL"), (" nvda ", "NVDA"), ("BRK.B", "BRK-B"), ("brk-b", "BRK-B"),
    ("walmex.mx", "WALMEX.MX"), ("WALMEX *", "WALMEX"), ("^gspc", "^GSPC"), ("ry.to", "RY.TO"),
])
def test_symbols_are_normalized_to_yahoo_form(raw, expected):
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "AAPL;rm", "../etc", "A" * 20, "<b>", "AAPL,MSFT"])
def test_garbage_is_not_a_symbol(raw):
    with pytest.raises(ValueError):
        normalize_symbol(raw)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def test_sma_and_bollinger():
    close = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(close, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    up, mid, low = bollinger(pd.Series([10.0] * 25))
    assert up.iloc[-1] == mid.iloc[-1] == low.iloc[-1] == 10.0          # no volatility, no width
    up, mid, low = bollinger(pd.Series(np.r_[[10.0] * 19, 30.0]))
    assert up.iloc[-1] - mid.iloc[-1] == pytest.approx(mid.iloc[-1] - low.iloc[-1])


def test_rsi_extremes_and_balance():
    assert rsi(pd.Series(np.arange(1.0, 40.0))).iloc[-1] == 100.0            # only gains
    assert rsi(pd.Series(np.arange(40.0, 1.0, -1))).iloc[-1] == pytest.approx(0.0)
    zigzag = pd.Series([10.0, 11.0] * 30)
    assert 40 < rsi(zigzag).iloc[-1] < 60                                     # balanced moves


def test_fibonacci_in_an_advance_measures_down_from_the_high():
    df = _bars(np.r_[np.linspace(100, 50, 50), np.linspace(50, 150, 100), np.linspace(150, 120, 20)])
    f = fibonacci(df)
    assert f["direction"] == "up"
    levels = {lv["ratio"]: lv["price"] for lv in f["levels"]}
    hi, lo = f["high"], f["low"]
    assert levels[0.0] == pytest.approx(hi) and levels[1.0] == pytest.approx(lo)
    assert levels[0.618] == pytest.approx(hi - 0.618 * (hi - lo))
    assert f["zone"] == "23.6%–38.2%"                                         # 120 sits in that band


def test_fibonacci_in_a_decline_measures_up_from_the_low():
    df = _bars(np.r_[np.linspace(100, 200, 60), np.linspace(200, 80, 100)])
    f = fibonacci(df)
    assert f["direction"] == "down"
    levels = {lv["ratio"]: lv["price"] for lv in f["levels"]}
    assert levels[0.0] == pytest.approx(f["low"]) and levels[1.0] == pytest.approx(f["high"])


def test_stats_on_a_known_series():
    df = _trend()
    window = df.iloc[-250:]
    s = stats(df, window)
    assert s["return_1y_pct"] == pytest.approx((1.002 ** 249 - 1) * 100)
    assert s["max_drawdown_pct"] == pytest.approx(0.0)                       # never falls
    assert s["volatility_pct"] == pytest.approx(0.0, abs=1e-6)               # constant growth
    assert s["trend"] == "up" and s["rsi14"] == 100.0
    assert s["from_high_pct"] < 0                                            # highs are 1% above closes


def test_chart_payload_covers_a_year_with_every_indicator_defined():
    p = chart_payload("TEST", _trend())
    n = len(p["bars"])
    assert 250 <= n <= 262                                                   # one calendar year of business days
    assert len(p["sma200"]) == len(p["sma50"]) == len(p["rsi"]) == len(p["bb_upper"]) == n
    assert p["bars"][-1]["time"] == p["as_of"] == "2026-10-14"
    assert p["currency"] == "USD" and len(p["fibonacci"]["levels"]) == 7
    json.dumps(clean(p), allow_nan=False)                                    # valid JSON, no NaN


def test_chart_payload_refuses_a_stub_history():
    with pytest.raises(MarketDataError):
        chart_payload("NEW", _bars([10.0] * 5))


def test_clean_makes_json_safe():
    assert clean({"a": float("nan"), "b": [np.float64(1.5), np.int64(2), np.bool_(True), float("inf")]}) == \
        {"a": None, "b": [1.5, 2, True, None]}


def test_sector_index_is_the_equal_weight_average_against_the_benchmark():
    a = _bars(np.r_[[100.0] * 300, [110.0]])          # +10% over the year
    b = _bars(np.r_[[100.0] * 300, [130.0]])          # +30%
    bench = _bars(np.r_[[100.0] * 300, [105.0]])      # +5%
    sec = sector_payload("Technology", {"AAA": a, "BBB": b}, benchmark=bench)
    assert sec["stats"]["return_1y_pct"] == pytest.approx(20.0)
    assert sec["stats"]["benchmark_1y_pct"] == pytest.approx(5.0)
    assert sec["stats"]["vs_benchmark_pct"] == pytest.approx(15.0)
    assert sec["series"]["AAA"][0]["value"] == 0.0                         # rebased to 0% at the start
    assert sec["members"]["BBB"]["vs_benchmark_pct"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# Fetching: cache, offline, rate limits
# ---------------------------------------------------------------------------

class FakeYahoo:
    def __init__(self, frames=None, info=None, fail=None):
        self.frames = frames or {}
        self.info_map = info or {}
        self.fail = list(fail or [])
        self.calls = []

    def history(self, symbol, start):
        self.calls.append(symbol)
        if self.fail:
            raise self.fail.pop(0)
        return self.frames.get(symbol, pd.DataFrame())

    def info(self, symbol):
        return self.info_map.get(symbol, {})


def test_prices_are_cached_on_disk(tmp_path):
    y = FakeYahoo({"AAPL": _trend()})
    md = MarketData(tmp_path, downloader=y.history, info_downloader=y.info)
    first = md.bars("aapl")
    second = MarketData(tmp_path, downloader=y.history).bars("AAPL")          # a new process reads the cache
    assert y.calls == ["AAPL"]
    pd.testing.assert_frame_equal(first, second, check_freq=False)
    assert second.attrs["currency"] == "USD"


def test_an_expired_cache_is_refreshed_and_offline_never_fetches(tmp_path):
    y = FakeYahoo({"AAPL": _trend()})
    md = MarketData(tmp_path, ttl_seconds=60, downloader=y.history)
    md.bars("AAPL")
    cache = tmp_path / "prices" / "AAPL.json"
    old = time.time() - 3600
    os.utime(cache, (old, old))
    md.bars("AAPL")
    assert y.calls == ["AAPL", "AAPL"]
    offline = MarketData(tmp_path, offline=True, downloader=y.history)
    offline.bars("AAPL")                                                    # stale is fine offline
    with pytest.raises(MarketDataError, match="offline"):
        offline.bars("MSFT")
    assert y.calls == ["AAPL", "AAPL"]


def test_unknown_ticker_is_a_clear_error(tmp_path):
    md = MarketData(tmp_path, downloader=FakeYahoo().history)
    with pytest.raises(MarketDataError, match="no prices"):
        md.bars("ZZZZZ")


def test_a_rate_limit_pauses_briefly_then_succeeds(tmp_path, _throttle_state):
    from trading_common.throttle import RateLimitedError

    y = FakeYahoo({"AAPL": _trend()}, fail=[RateLimitedError("429 Too Many Requests")])
    df = MarketData(tmp_path, downloader=y.history).bars("AAPL")
    assert len(df) and y.calls == ["AAPL", "AAPL"]
    assert CHART_POLICY.base_pause <= _throttle_state[0] <= CHART_POLICY.base_pause + 1
    assert CHART_POLICY.wait_when_paused is False                            # a page never waits minutes


def test_sector_comes_from_the_ranking_else_yahoo(tmp_path):
    y = FakeYahoo(info={"WALMEX.MX": {"sector": "Consumer Defensive", "shortName": "Walmex"},
                        "SPY": {"quoteType": "ETF", "shortName": "SPDR S&P 500"}})
    md = MarketData(tmp_path, downloader=y.history, info_downloader=y.info)
    known = {"NVDA": {"sector": "Technology", "name": "NVIDIA"}}
    assert md.meta("NVDA", known) == {"sector": "Technology", "name": "NVIDIA"}
    assert md.meta("WALMEX.MX", known)["sector"] == "Consumer Defensive"
    assert md.meta("SPY")["sector"] == "ETF / Fund"


# ---------------------------------------------------------------------------
# Watchlist (Workflow)
# ---------------------------------------------------------------------------

@pytest.fixture
def wf(tmp_path):
    shutil.copytree(REPO / "configs/us_equities", tmp_path / "configs/us_equities")
    paths = default_paths(tmp_path)
    y = FakeYahoo({s: _trend() for s in ("NVDA", "MSFT", "FRO", "AAPL", "^GSPC")},
                  info={"AAPL": {"sector": "Technology", "shortName": "Apple Inc."}})
    md = MarketData(paths.app_dir / "cache" / "market", downloader=y.history, info_downloader=y.info)
    w = Workflow(paths, JobManager(paths.jobs_dir, cwd=tmp_path, popen=lambda *a, **k: None), market=md)
    w.fake = y
    return w


def _ranking(paths):
    paths.ranking_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"rank": [2, 1, 3], "ticker": ["MSFT", "FRO", "BRK.B"],
                  "company_name": ["Microsoft Corporation - Common Stock", "Frontline Plc Ordinary Shares", "Berkshire"],
                  "sector": ["Technology", "Energy", "Financial Services"],
                  "eligible_for_allocation": [True, True, False]}).to_csv(paths.ranking_csv, index=False)


def test_default_watchlist_is_holdings_then_the_top_of_the_ranking(wf):
    assert wf.watchlist() == []
    _ranking(wf.paths)
    assert wf.watchlist() == ["FRO", "MSFT"]                                  # rank order, eligible only
    wf.init_holdings()                                                       # template: NVDA, AAPL, WALMEX (BMV)
    assert wf.watchlist() == ["NVDA", "AAPL", "WALMEX.MX", "FRO", "MSFT"]


def test_add_remove_reset_and_persist(wf):
    _ranking(wf.paths)
    assert wf.add_to_watchlist("aapl")[-1] == "AAPL"
    assert wf.add_to_watchlist("AAPL") == ["FRO", "MSFT", "AAPL"]            # no duplicates
    assert json.loads(wf.paths.settings_json.read_text())["watchlist"] == ["FRO", "MSFT", "AAPL"]
    assert wf.remove_from_watchlist("msft") == ["FRO", "AAPL"]
    with pytest.raises(InvalidRequest, match="no prices"):
        wf.add_to_watchlist("ZZZZZ")                                         # Yahoo has nothing for it
    with pytest.raises(InvalidRequest, match="not a ticker"):
        wf.add_to_watchlist("<script>")
    assert wf.reset_watchlist() == ["FRO", "MSFT"]
    assert wf.settings.watchlist is None


def test_watchlist_has_a_ceiling(wf, monkeypatch):
    import trading_app.services as svc

    monkeypatch.setattr(svc, "WATCHLIST_MAX", 2)
    wf.add_to_watchlist("NVDA")
    wf.add_to_watchlist("MSFT")
    with pytest.raises(InvalidRequest, match="up to 2"):
        wf.add_to_watchlist("AAPL")


def test_chart_and_sectors_from_the_workflow(wf):
    _ranking(wf.paths)
    wf.add_to_watchlist("AAPL")
    chart = wf.market_chart("MSFT")
    assert chart["name"] == "Microsoft Corporation" and chart["sector"] == "Technology"
    res = wf.market_sectors()
    names = [s["sector"] for s in res["sectors"]]
    assert names == ["Technology", "Energy"]                                 # biggest group first
    assert set(res["sectors"][0]["series"]) == {"MSFT", "AAPL"}
    assert res["benchmark"] == "S&P 500" and res["errors"] == {}
    json.dumps(res, allow_nan=False)


def test_a_failing_symbol_does_not_sink_the_sector_view(wf):
    wf._save_watchlist(["NVDA", "GONE"])
    res = wf.market_sectors()
    assert "GONE" in res["errors"] and [s["sector"] for s in res["sectors"]] == ["Other"]


# ---------------------------------------------------------------------------
# Job progress for the selection run
# ---------------------------------------------------------------------------

LOG = ("INFO Stage 1: cleaned universe = 4757 tickers.\n"
       "Stage 2: prices:   0%|          | 0/4757 [00:00<?, ?it/s]\r"
       "Stage 2: prices: 100%|##########| 4757/4757 [32:10<00:00,  2.46it/s]\n"
       "Stage 3: fundamentals:  10%|#         | 50/500 [00:20<03:00,  2.50it/s]\n")


def test_selection_progress_reads_the_latest_frame_of_each_stage():
    rows = selection_progress(LOG)
    assert [r["name"] for r in rows] == ["Stage 2: prices", "Stage 3: fundamentals"]
    assert rows[0]["done"] and rows[0]["elapsed"] == "32:10"
    assert rows[1] == {"name": "Stage 3: fundamentals", "pct": 10, "n": 50, "total": 500, "elapsed": "00:20",
                       "eta": "03:00", "done": False, "pausing": False}


def test_selection_progress_flags_a_pause_until_progress_resumes():
    paused = LOG + "WARNING yfinance is rate limiting (429); pausing 30s backing off, then continuing\n"
    assert selection_progress(paused)[-1]["pausing"] is True
    resumed = paused + "Stage 3: fundamentals:  11%|#         | 55/500 [00:52<03:00,  2.50it/s]\r"
    assert selection_progress(resumed)[-1]["pausing"] is False
    assert selection_progress("") == []


def test_a_finished_job_process_is_reaped(tmp_path):
    class Proc:
        pid, waited = 4242, []

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waited.append(timeout)
            return 0

    proc = Proc()
    jm = JobManager(tmp_path / "jobs", cwd=tmp_path, popen=lambda argv, **kw: proc)
    job = jm.start("selection", "t", ["true"])
    (jm.job_dir(job.id) / "exit_code").write_text("0")
    assert jm.get(job.id).state == "succeeded"
    assert proc.waited == [5]                                                 # collected, not left a zombie


# ---------------------------------------------------------------------------
# API and page
# ---------------------------------------------------------------------------

@pytest.fixture
def client(wf):
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from trading_app.web.app import create_app

    return TestClient(create_app(wf))


def test_api_watchlist_round_trip(client, wf):
    _ranking(wf.paths)
    assert client.get("/api/market/watchlist").json() == {"symbols": ["FRO", "MSFT"], "custom": False, "max": 24}
    r = client.post("/api/market/watchlist", json={"add": "nvda"})
    assert r.status_code == 200 and r.json()["symbols"] == ["FRO", "MSFT", "NVDA"] and r.json()["custom"]
    assert client.post("/api/market/watchlist", json={"remove": "FRO"}).json()["symbols"] == ["MSFT", "NVDA"]
    bad = client.post("/api/market/watchlist", json={"add": "ZZZZZ"})
    assert bad.status_code == 400 and "no prices" in bad.json()["detail"]
    assert client.post("/api/market/watchlist", json={}).status_code == 400
    assert client.post("/api/market/watchlist", json={"reset": True}).json()["custom"] is False


def test_api_chart_and_sectors(client, wf):
    _ranking(wf.paths)
    chart = client.get("/api/market/chart/msft")
    assert chart.status_code == 200
    body = chart.json()
    assert body["symbol"] == "MSFT" and body["sector"] == "Technology" and len(body["bars"]) > 200
    assert client.get("/api/market/chart/%3Cscript%3E").status_code == 400
    assert client.get("/api/market/chart/ZZZZZ").status_code == 502       # upstream had nothing
    sectors = client.get("/api/market/sectors").json()
    assert [s["sector"] for s in sectors["sectors"]] == ["Energy", "Technology"]


def test_cross_site_watchlist_changes_are_refused(client):
    r = client.post("/api/market/watchlist", json={"add": "NVDA"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_dashboard_page_loads_the_charts_and_the_brand(client):
    page = client.get("/").text
    for needle in ('id="markets"', "/static/vendor/lightweight-charts.standalone.production.js",
                   "/static/app.js", "/static/brand/logo-mark.webp", "Finance Bull", "not forecasts"):
        assert needle in page
    for asset in ("/static/app.js", "/static/style.css", "/static/brand/logo-full.webp",
                  "/static/brand/logo-mark.webp", "/static/brand/favicon-32.png",
                  "/static/vendor/lightweight-charts.standalone.production.js"):
        assert client.get(asset).status_code == 200, asset


def test_vendored_chart_library_is_the_pinned_release():
    import hashlib

    lib = REPO / "src/trading_app/web/static/vendor/lightweight-charts.standalone.production.js"
    text = lib.read_text(encoding="utf-8")
    assert "5.2.1" in text[:400]
    # Checked against the npm package's integrity hash when it was vendored.
    assert hashlib.sha256(lib.read_bytes()).hexdigest() == "e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf"
    assert (lib.parent / "lightweight-charts.LICENSE.txt").exists()


def test_job_page_shows_stage_progress_and_highlights_pauses(client, wf):
    class Running:
        pid = 4242

        def poll(self):
            return None

    wf.jobs._popen = lambda argv, **kw: Running()
    job = wf.jobs.start("selection", "Full-universe stock selection", ["true"])
    wf.jobs.log_path(job.id).write_text(
        LOG + "WARNING yfinance is rate limiting (429); pausing 30s backing off, then continuing\n")
    page = client.get(f"/jobs/{job.id}").text
    assert "Stage 3: fundamentals" in page and "50 / 500" in page and 'style="width: 10%"' in page
    assert '<span class="hl">' in page and "nothing is skipped" in page
    api = client.get(f"/api/jobs/{job.id}").json()
    assert api["progress"][1]["pausing"] is True
