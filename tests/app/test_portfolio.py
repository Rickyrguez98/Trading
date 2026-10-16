"""Portfolio page: PDF import, consolidation, live prices, CSV and the dashboard link.

Uses the synthetic confirmation PDFs and a fake Yahoo, so nothing touches the
network.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pdfplumber")

from trading_app.services import PortfolioError, Workflow, default_paths  # noqa: E402
from trading_app.services.jobs import JobManager  # noqa: E402
from trading_app.services.market import MarketData  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests/fixtures/monitoring"
BUYS = (FIX / "confirmation_buys.pdf").read_bytes()
SELLS = (FIX / "confirmation_sells.pdf").read_bytes()


def _bars(n=520, start=100.0):
    closes = start * np.exp(np.cumsum(np.random.default_rng(3).normal(0.0005, 0.02, n)))   # seeded random walk
    idx = pd.bdate_range(end=pd.Timestamp("2026-10-14"), periods=n)
    return pd.DataFrame({"open": closes, "high": closes * 1.01, "low": closes * 0.99, "close": closes,
                         "volume": 1e6}, index=idx)


class FakeYahoo:
    def __init__(self):
        self.quote_calls = []
        self.history_starts = []
        self.prices = {"AAPL": 260.0, "BRK-B": 480.0}

    def history(self, symbol, start):
        self.history_starts.append((symbol, start))
        return _bars()

    def quote(self, symbol):
        self.quote_calls.append(symbol)
        p = self.prices.get(symbol)
        return {} if p is None else {"price": p, "prev_close": p * 0.99, "change_pct": 1.0,
                                     "as_of": "2026-10-16T10:45:00-04:00"}


@pytest.fixture
def wf(tmp_path):
    shutil.copytree(REPO / "configs/us_equities", tmp_path / "configs/us_equities")
    paths = default_paths(tmp_path)
    y = FakeYahoo()
    md = MarketData(paths.app_dir / "cache" / "market", downloader=y.history, info_downloader=lambda s: {},
                    quote_downloader=y.quote)
    w = Workflow(paths, JobManager(paths.jobs_dir, cwd=tmp_path, popen=lambda *a, **k: None), market=md)
    w.fake = y
    return w


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def test_import_many_pdfs_dedupes_and_reports_bad_files(wf):
    res = wf.portfolio.import_pdfs([("buys.pdf", BUYS), ("sells.pdf", SELLS), ("notes.txt", b"hello"),
                                    ("statement.pdf", (FIX / "drivewealth_statement.pdf").read_bytes())])
    assert (res.added, res.duplicates) == (7, 0)
    assert [f.error is None for f in res.files] == [True, True, False, False]
    assert "not a PDF" in res.files[2].error and "trade confirmation" in res.files[3].error
    again = wf.portfolio.import_pdfs([("buys-copy.pdf", BUYS)])
    assert (again.added, again.duplicates) == (0, 4)
    assert "Read 1 of 1 file(s): 4 trade(s), 0 new, 4 already imported" == again.message()
    assert json.loads(wf.portfolio.report_path.read_text())["duplicates"] == 4


def test_rows_summary_and_live_prices(wf):
    wf.portfolio.import_pdfs([("b.pdf", BUYS), ("s.pdf", SELLS)])
    pf = wf.portfolio
    view = pf.view()
    rows = pf.rows(view, pf.quotes(view))
    by = {(r["symbol"], r["status"]): r for r in rows}
    aapl_open = by[("AAPL", "OPEN")]
    assert aapl_open["shares"] == pytest.approx(1.4) and aapl_open["current_price"] == 260.0
    assert aapl_open["market_value"] == pytest.approx(364.0)
    assert aapl_open["pl"] == pytest.approx(364.0 - 350.14)              # cost includes the buy commission
    nvda = by[("NVDA", "CLOSED")]
    assert nvda["pl"] == pytest.approx(84.80 - 90.22) and nvda["return_pct"] < 0
    assert by[("TSLA", "UNMATCHED_SELL")]["pl"] is None
    s = pf.summary(view, rows)
    assert s["open_positions"] == 2 and s["closed_trades"] == 2 and (s["wins"], s["losses"]) == (1, 1)
    assert s["realized_pl"] == pytest.approx(24.90 - 5.42)
    assert s["total_pl"] == pytest.approx(s["realized_pl"] + s["unrealized_pl"])
    assert sorted(wf.fake.quote_calls) == ["AAPL", "BRK-B"]               # only open positions, Yahoo symbols


def test_quotes_are_reused_for_five_minutes(wf):
    md = wf.market
    md.quotes(["AAPL"])
    md.quotes(["AAPL"])
    assert wf.fake.quote_calls == ["AAPL"]
    book = json.loads((md.cache_dir / "quotes.json").read_text())
    book["AAPL"]["fetched"] = time.time() - 301
    (md.cache_dir / "quotes.json").write_text(json.dumps(book))
    md.quotes(["AAPL"])
    assert wf.fake.quote_calls == ["AAPL", "AAPL"]
    assert "error" in md.quotes(["ZZZZ"])["ZZZZ"]                         # unpriced, not a crash


def test_charts_reach_back_to_the_first_purchase(wf):
    from datetime import date

    wf.portfolio.import_pdfs([("b.pdf", BUYS)])
    data = wf.portfolio.chart_data()
    assert data["symbols"] == ["AAPL", "BRK-B", "NVDA"] and data["open"] == data["symbols"]
    assert data["since"]["AAPL"] == "2026-03-02"
    assert data["markers"]["AAPL"] == [{"time": "2026-03-02", "side": "BUY", "shares": 2.4, "price": 250.0}]
    assert data["avg_cost"]["AAPL"] == pytest.approx(600.24 / 2.4)
    wf.market_chart("AAPL", since=date(2024, 1, 2))
    assert wf.fake.history_starts[-1][1] < date(2024, 1, 2)                  # fetched far enough back


def test_use_as_holdings_and_clear_keep_backups(wf):
    with pytest.raises(PortfolioError):
        wf.portfolio.write_holdings(wf.paths.resolve(wf.settings.holdings_csv))
    wf.init_holdings()
    wf.portfolio.import_pdfs([("b.pdf", BUYS)])
    dest = wf.paths.resolve(wf.settings.holdings_csv)
    assert wf.portfolio.write_holdings(dest) == 3
    assert list(dest.parent.glob("my_holdings.backup-*.csv"))
    assert "BRK.B" in dest.read_text()
    backup = wf.portfolio.clear()
    assert backup.exists() and not wf.portfolio.ledger_path.exists()


def test_dashboard_defaults_to_your_own_stocks(wf):
    wf.init_holdings()                                                      # template: NVDA, AAPL, WALMEX
    assert wf.watchlist()[:3] == ["NVDA", "AAPL", "WALMEX.MX"]
    wf.portfolio.import_pdfs([("b.pdf", BUYS)])
    assert wf.watchlist() == ["AAPL", "BRK-B", "NVDA"]                    # the confirmations win (same day: A-Z)
    assert wf.dashboard()["holdings_source"] == "trade confirmations"


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

@pytest.fixture
def client(wf):
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from trading_app.web.app import create_app

    return TestClient(create_app(wf))


def test_portfolio_page_before_and_after_import(client):
    empty = client.get("/portfolio").text
    assert "Import your trade confirmations" in empty and 'id="ops"' not in empty
    r = client.post("/portfolio/import", files=[("files", ("b.pdf", BUYS, "application/pdf")),
                                                ("files", ("s.pdf", SELLS, "application/pdf"))],
                    follow_redirects=False)
    assert r.status_code == 303 and "7%20new" in r.headers["location"]
    page = client.get("/portfolio").text
    for needle in ('id="ops"', "AAPL", "BRK.B", "purchase not imported", "/static/portfolio.js",
                   'data-mode="portfolio"', "Download CSV"):
        assert needle in page, needle
    assert page.count('data-open="') == 2


def test_a_batch_of_unreadable_files_is_an_error(client):
    r = client.post("/portfolio/import", files=[("files", ("x.pdf", b"%PDF-1.4 broken", "application/pdf"))],
                    follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_quotes_api_and_csv_downloads(client):
    client.post("/portfolio/import", files=[("files", ("b.pdf", BUYS, "application/pdf")),
                                            ("files", ("s.pdf", SELLS, "application/pdf"))])
    q = client.get("/api/portfolio/quotes").json()
    assert q["refresh_seconds"] == 300 and {o["symbol"] for o in q["operations"]} == {"AAPL", "BRK.B"}
    assert q["summary"]["market_value"] == pytest.approx(1.4 * 260 + 0.1 * 480)
    r = client.get("/portfolio/operations.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="portfolio_operations_' in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["status"] for row in rows} == {"OPEN", "CLOSED", "UNMATCHED_SELL"}
    assert next(r for r in rows if r["status"] == "OPEN" and r["symbol"] == "AAPL")["current_price"] == "260"
    raw = client.get("/portfolio/executions.csv").text
    assert raw.count("\n") == 8 and "SYNTHETIC" not in raw                   # 7 rows + header; no holder name
    assert "…0042" in raw                                                   # account masked


def test_portfolio_chart_endpoints(client):
    client.post("/portfolio/import", files=[("files", ("b.pdf", BUYS, "application/pdf"))])
    data = client.get("/api/portfolio/chart-data").json()
    assert "BRK-B" in data["symbols"]
    assert client.get("/api/market/chart/AAPL?since=2026-03-02").status_code == 200
    assert client.get("/api/market/chart/AAPL?since=yesterday").status_code == 400
    sectors = client.get("/api/market/sectors?symbols=AAPL,NVDA").json()
    assert sum(len(s["series"]) for s in sectors["sectors"]) == 2


def test_portfolio_writes_are_same_origin_only(client):
    r = client.post("/portfolio/clear", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Next month's range and sell levels
# ---------------------------------------------------------------------------

def _noisy(n=520, seed=1, drift=0.0):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    idx = pd.bdate_range(end=pd.Timestamp("2026-10-14"), periods=n)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1e6}, index=idx)


def test_outlook_columns_per_horizon_are_consistent():
    from trading_app.services.outlook import HORIZONS, stock_outlook

    bars = _noisy(1500)
    o = stock_outlook(bars, key="TEST")
    assert o["status"] == "ok" and o["as_of"] == "2026-10-14" and set(o["horizons"]) == {"21", "63", "126", "252"}
    last = bars["close"].iloc[-1]
    for h in HORIZONS:
        x = o["horizons"][str(h)]
        assert x["min"] < last < x["max"] and x["min"] < x["median"] < x["max"]
        assert x["trend"] in ("up", "down", "flat") and 0 <= x["p_up"] <= 1
    widths = [o["horizons"][str(h)]["max"] - o["horizons"][str(h)]["min"] for h in HORIZONS]
    assert widths == sorted(widths)                                          # longer horizon, wider range
    assert stock_outlook(bars, key="TEST") == o                              # seeded: same answer every time


def test_trends_are_only_validated_with_enough_independent_evidence():
    from trading_app.services.outlook import stock_outlook

    o = stock_outlook(_noisy(1500), key="T")
    for h in ("126", "252"):                                                 # 24 overlapping windows: < 8 independent
        assert o["horizons"][h]["testable"] is False and o["horizons"][h]["validated"] is False
    assert o["horizons"]["21"]["tests"] and o["horizons"]["21"]["testable"] is True
    flat = stock_outlook(_noisy(1500, seed=9, drift=0.0), key="F")
    if flat["trend_per_year_used_pct"] == 0:
        assert all(x["trend"] == "flat" for x in flat["horizons"].values())  # no drift left -> no direction


def test_month_outlook_is_honest_about_short_histories():
    from trading_app.services.outlook import stock_outlook

    assert stock_outlook(_noisy(40), key="NEW")["status"] == "insufficient_history"
    short = stock_outlook(_noisy(150), key="IPO")
    assert short["status"] == "ok" and short["horizons"]["21"]["tests"] is None   # shown, but not back-tested


def test_recommendation_balances_price_and_chance_of_filling():
    from trading_app.services.outlook import MIN_FILL, exit_plan, month_paths

    bars = _noisy(1500)
    paths = month_paths(bars, key="T", with_trend=False)
    price = float(bars["close"].iloc[-1])
    plan = exit_plan(paths, price=price, cost_per_share=price * 0.8, shares=2, commission_rate=0.0025,
                     trend_used=False)
    assert plan["plan"] == "limit" and plan["limit"] > price and plan["p_fill"] >= MIN_FILL
    assert plan["stop"] < price and plan["p_stop"] == pytest.approx(0.10, abs=0.01)
    assert plan["exit_vs_cost_pct"] > plan["now_vs_cost_pct"] > 0            # waiting adds to the gain
    assert plan["confidence"].startswith("no proven trend")
    # A higher or lower limit than the chosen one scores worse on price x chance.
    run_max = paths.max(axis=1)
    def score(level):
        return (level - price) * float(np.mean(run_max >= level))
    assert score(plan["limit"]) >= max(score(plan["limit"] * 1.05), score(price + (plan["limit"] - price) * 0.5))
    flat = exit_plan(np.full((100, 21), price), price=price, cost_per_share=price, shares=1, commission_rate=0,
                     trend_used=False)
    assert flat["plan"] == "sell_now"                                        # nothing to wait for


def test_outlooks_are_computed_once_per_trading_day(wf, monkeypatch):
    import trading_app.services.portfolio as pmod

    calls = []
    real = pmod.stock_outlook
    monkeypatch.setattr(pmod, "stock_outlook", lambda bars, key: calls.append(key) or real(bars, key=key))
    wf.portfolio.import_pdfs([("b.pdf", BUYS)])
    first = wf.portfolio.outlooks()
    again = wf.portfolio.outlooks()
    assert sorted(calls) == ["AAPL", "BRK-B", "NVDA"] and first == again
    assert set(first) == {"AAPL", "BRK.B", "NVDA"} and first["AAPL"]["status"] == "ok"


def test_outlook_api_csv_and_columns(client):
    client.post("/portfolio/import", files=[("files", ("b.pdf", BUYS, "application/pdf")),
                                            ("files", ("s.pdf", SELLS, "application/pdf"))])
    res = client.get("/api/portfolio/outlook").json()
    assert res["horizons"] == {"21": "1M", "63": "3M", "126": "6M", "252": "1Y"}
    assert set(res["symbols"]) == {"AAPL", "BRK.B"} and 0 < res["commission_rate"] < 0.01
    plan = next(v for k, v in res["plans"].items() if k.startswith("AAPL|"))
    assert plan["plan"] in ("limit", "sell_now") and plan["now_vs_cost_pct"] is not None
    rows = list(csv.DictReader(io.StringIO(client.get("/portfolio/operations.csv").text)))
    aapl = next(r for r in rows if r["symbol"] == "AAPL" and r["status"] == "OPEN")
    closed = next(r for r in rows if r["status"] == "CLOSED")
    for label in ("1M", "3M", "6M", "1Y"):
        assert aapl[f"trend_{label}"] in ("up", "down", "flat") and float(aapl[f"expected_{label}"]) > 0
        assert float(aapl[f"min_{label}"]) < float(aapl[f"max_{label}"])
    assert aapl["recommendation"] and aapl["outlook_as_of"] == "2026-10-14" and closed["recommendation"] == ""
    page = client.get("/portfolio").text
    assert page.count('class="hz-group"') == 4 and "Short-term recommendation" in page
    assert page.count('data-k="trend"') == 2 * 4 and page.count("js-rec") == 2 and "not financial advice" in page
