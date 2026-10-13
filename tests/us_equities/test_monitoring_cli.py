"""End-to-end: trading-monitor with market data injected (no network)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from asset_selection.monitoring.config import load_config
from asset_selection.monitoring.holdings import aggregate_lots, load_holdings_csv
from asset_selection.monitoring.market_data import FX_SYMBOL, EarningsInfo, StaticMarketData
from asset_selection.pipelines import run_portfolio_monitor as mon

from .monitoring_fixtures import constant_frame, price_frame, trend_frame


def _market() -> StaticMarketData:
    return StaticMarketData(
        histories={
            "NVDA": price_frame(1300, start=60.0, mu=0.0008, sigma=0.02, seed=5),
            "AAPL": price_frame(1300, start=150.0, mu=0.0004, sigma=0.015, seed=6),
            "MSFT": trend_frame("up", n=1300),
            "WALMEX.MX": price_frame(1300, start=70.0, mu=-0.0002, sigma=0.012, seed=8),
            FX_SYMBOL: constant_frame(18.25, n=1300),
        },
        earnings={"NVDA": EarningsInfo(next_date=date(2026, 11, 17),
                                       history=[(date(2026, 8, 26), 2.09, 2.22, 6.2)])},
    )


def _config(tmp_path: Path, **forecast) -> Path:
    cfg = {
        "run": {"output_dir": str(tmp_path / "reports"), "processed_dir": str(tmp_path / "processed"),
                "cache_dir": str(tmp_path / "cache"), "selection_results_csv": None,
                "selection_summary_json": None},
        "forecast": {"n_paths": 400, "validation_origins": 12, **forecast},
    }
    p = tmp_path / "monitoring.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _selection_csv(tmp_path: Path) -> Path:
    p = tmp_path / "ranking.csv"
    pd.DataFrame([
        {"rank": 1, "ticker": "NVDA", "company_name": "NVIDIA", "sector": "Technology",
         "fundamentals_score": 62.0, "effective_sentiment_score": 55.0,
         "effective_sentiment_confidence": 0.5, "candidate_role": "core_candidate",
         "eligible_for_allocation": True},
        {"rank": 2, "ticker": "MSFT", "company_name": "Microsoft", "sector": "Technology",
         "fundamentals_score": 35.0, "candidate_role": "watchlist_only",
         "eligible_for_allocation": False},
    ]).to_csv(p, index=False)
    return p


def _holdings(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "holdings.csv"
    p.write_text(text, encoding="utf-8")
    return p


HOLDINGS = (
    "ticker,shares,avg_cost,currency,market,purchase_date\n"
    "NVDA *,10,1500,MXN,SIC,2025-03-14\n"
    "AAPL,3,180,USD,US,2025-06-02\n"
    "MSFT,2,300,USD,US,2025-01-10\n"
    "WALMEX *,40,62.15,MXN,BMV,14/01/2025\n"
)


@pytest.fixture
def patched_market(monkeypatch):
    market = _market()
    monkeypatch.setattr(mon, "YFinanceMarketData", lambda *a, **k: market)
    return market


def test_end_to_end_writes_valid_outputs(tmp_path, patched_market, capsys):
    code = mon.main([
        "--holdings", str(_holdings(tmp_path, HOLDINGS)), "--config", str(_config(tmp_path)),
        "--selection-results", str(_selection_csv(tmp_path)), "--cash", "20000", "--log-level", "ERROR",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "Portfolio monitor" in out and "Report:" in out

    summary = json.loads((tmp_path / "reports" / "portfolio_monitor_summary.json").read_text())
    # Strict JSON: no NaN/Infinity literals anywhere.
    json.dumps(summary, allow_nan=False)
    positions = {p["ticker"]: p for p in summary["positions"]}
    assert set(positions) == {"NVDA", "AAPL", "MSFT", "WALMEX"}
    assert summary["reporting_currency"] == "MXN" and summary["fx_usdmxn"] == 18.25

    # MSFT's fundamentals collapsed (35 < 40): the thesis is broken -> SELL all of it.
    msft = positions["MSFT"]["recommendation"]
    assert msft["action"] == "SELL" and msft["shares_delta"] == -2
    # WALMEX is a Mexican listing: no thesis check, analysed on price only.
    assert positions["WALMEX"]["recommendation"]["thesis_status"] == "UNKNOWN"
    # Weights sum to 100% (each is rounded to 4 decimals in the JSON).
    assert sum(p["valuation"]["weight"] for p in summary["positions"]) == pytest.approx(1.0, abs=1e-3)

    md = (tmp_path / "reports" / "portfolio_monitor.md").read_text()
    for needle in ("Recommended actions", "Positions — percentage and money",
                   "Does the forecast beat a random walk?", "not financial advice"):
        assert needle in md

    orders = pd.read_csv(tmp_path / "processed" / "monitor_orders.csv")
    assert "MSFT" in set(orders["ticker"])
    sell = orders[orders["ticker"] == "MSFT"].iloc[0]
    assert sell["side"] == "SELL" and sell["shares"] == 2


def test_values_reconcile_in_the_reporting_currency(tmp_path, patched_market):
    mon.main(["--holdings", str(_holdings(tmp_path, HOLDINGS)), "--config", str(_config(tmp_path)),
              "--log-level", "ERROR", "--no-validation"])
    s = json.loads((tmp_path / "reports" / "portfolio_monitor_summary.json").read_text())
    total = sum(p["valuation"]["market_value_reporting"] for p in s["positions"])
    assert s["totals"]["market_value"] == pytest.approx(total, abs=0.05)
    nvda = next(p for p in s["positions"] if p["ticker"] == "NVDA")
    last_usd = float(patched_market.histories["NVDA"]["close"].iloc[-1])
    assert nvda["valuation"]["market_value_reporting"] == pytest.approx(10 * last_usd * 18.25, rel=1e-6)


def test_no_selection_results_degrades_the_run(tmp_path, patched_market):
    mon.main(["--holdings", str(_holdings(tmp_path, HOLDINGS)), "--config", str(_config(tmp_path)),
              "--log-level", "ERROR", "--no-validation"])
    s = json.loads((tmp_path / "reports" / "portfolio_monitor_summary.json").read_text())
    assert s["status"] == "DEGRADED"
    assert any("thesis could not be checked" in r for r in s["status_reasons"])


def test_pdf_statement_through_the_cli(tmp_path, patched_market):
    pytest.importorskip("pdfplumber")
    market = patched_market
    market.histories["GFNORTEO.MX"] = market.histories["WALMEX.MX"]
    code = mon.main(["--holdings", "tests/fixtures/monitoring/gbm_statement.pdf",
                     "--config", str(_config(tmp_path)), "--log-level", "ERROR", "--no-validation"])
    assert code == 0
    s = json.loads((tmp_path / "reports" / "portfolio_monitor_summary.json").read_text())
    ex = s["sources"]["extraction"]
    assert ex["statement_type"] == "gbm" and ex["lots"] == 4
    review = Path(ex["review_csv"])
    assert review.exists() and len(pd.read_csv(review)) == 4
    md = (tmp_path / "reports" / "portfolio_monitor.md").read_text()
    assert "Statement extraction" in md and "review" in md


def test_bad_holdings_exit_2(tmp_path, patched_market, capsys):
    bad = _holdings(tmp_path, "ticker,avg_cost\nAAPL,1\n")
    assert mon.main(["--holdings", str(bad), "--config", str(_config(tmp_path))]) == 2
    assert "Input error" in capsys.readouterr().err


def test_nothing_priced_is_invalid_and_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "YFinanceMarketData", lambda *a, **k: StaticMarketData({}))
    code = mon.main(["--holdings", str(_holdings(tmp_path, "ticker,shares,avg_cost\nZZZZ,1,10\n")),
                     "--config", str(_config(tmp_path)), "--log-level", "ERROR"])
    assert code == 2


def test_price_pins_are_repeatable_and_win(tmp_path, patched_market):
    mon.main(["--holdings", str(_holdings(tmp_path, HOLDINGS)), "--config", str(_config(tmp_path)),
              "--price", "AAPL=999", "--price", "MSFT=111", "--fx", "USDMXN=20",
              "--log-level", "ERROR", "--no-validation"])
    s = json.loads((tmp_path / "reports" / "portfolio_monitor_summary.json").read_text())
    pos = {p["ticker"]: p for p in s["positions"]}
    assert pos["AAPL"]["valuation"]["last_price"] == 999.0
    assert pos["MSFT"]["valuation"]["last_price"] == 111.0
    assert s["fx_usdmxn"] == 20.0


@pytest.mark.parametrize("bad", [["--price", "AAPL"], ["--price", "AAPL=-1"], ["--fx", "EURUSD=1.1"]])
def test_bad_pins_exit_2(tmp_path, patched_market, bad):
    assert mon.main(["--holdings", str(_holdings(tmp_path, HOLDINGS)),
                     "--config", str(_config(tmp_path)), *bad]) == 2


def test_targets_are_renormalised_over_what_is_held(tmp_path):
    lots, _ = load_holdings_csv(_holdings(tmp_path, HOLDINGS))
    holdings = aggregate_lots(lots)
    plan = tmp_path / "portfolio_balanced.csv"
    pd.DataFrame({"ticker": ["NVDA", "AAPL", "VOO", "BND", "CASH"],
                  "realized_weight_pct": [10.0, 10.0, 36.0, 20.0, 5.0]}).to_csv(plan, index=False)
    targets, missing = mon.load_targets(str(plan), holdings)
    assert targets == {"NVDA": 0.5, "AAPL": 0.5}
    assert missing == ["BND", "VOO"]


def test_monitor_config_ships_valid():
    cfg = load_config("configs/us_equities/monitoring.yaml")
    assert cfg.forecast.backend == "bootstrap" and cfg.run.reporting_currency == "MXN"


def test_config_validation_reports_every_problem():
    from asset_selection.monitoring.config import MonitoringConfigError
    with pytest.raises(MonitoringConfigError) as exc:
        load_config({"forecast": {"backend": "magic", "horizons": [], "n_paths": 10},
                     "decision": {"fundamentals_broken": 60, "fundamentals_weak": 50}})
    assert len(exc.value.problems) >= 4


def test_selection_cli_processed_dir_keeps_the_main_ranking_safe():
    """--processed-dir must redirect the results CSV, or a refresh clobbers the ranking."""
    from asset_selection.pipelines.run_asset_selection import _parse_args
    args = _parse_args(["--tickers", "AAPL", "--processed-dir", "/tmp/x"])
    assert args.processed_dir == "/tmp/x"
