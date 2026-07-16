"""End-to-end pipeline tests with scripted (offline) providers."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from binary_options_selection.pipelines.run_binary_options_selection import (
    build_arg_parser,
    main,
    run_pipeline,
)
from binary_options_selection.validation.output_validation import validate_outputs
from binary_options_selection.validation.run_status import (
    RUN_DEGRADED,
    RUN_INVALID,
    RUN_PARTIAL,
    RUN_VALID,
)

from tests.bo_fixtures import (
    NOW,
    ScriptedProvider,
    downtrend,
    flat,
    make_candles,
    make_config,
    make_series,
    trend_volume,
    uptrend,
)


def _cfg(tmp_path, tickers, horizons=(5, 15, 60)):
    cfg = make_config()
    cfg.universe.mode = "custom"
    cfg.universe.tickers = list(tickers)
    cfg.universe.benchmark_symbol = "SPY"
    cfg.horizons.minutes = list(horizons)
    cfg.run.output_dir = str(tmp_path / "reports")
    cfg.run.processed_dir = str(tmp_path / "processed")
    return cfg


def _healthy_scripts(symbols):
    steps = {"UP": uptrend, "DOWN": downtrend, "FLAT": flat, "SPY": flat}
    def build(sym):
        step = steps.get(sym, uptrend)
        vol = trend_volume if step in (uptrend, downtrend) else (lambda i: 50_000.0)
        return make_series(sym, make_candles(step=step, volume=vol))
    return {sym: (lambda s=sym: build(s)) for sym in [*symbols, "SPY"]}


def test_end_to_end_valid_run(tmp_path):
    cfg = _cfg(tmp_path, ["UP", "DOWN", "FLAT"])
    provider = ScriptedProvider(_healthy_scripts(["UP", "DOWN", "FLAT"]))
    res = run_pipeline(cfg, provider=provider, now=NOW)
    a = res["assessment"]
    assert a.run_status == RUN_VALID, a.reasons

    rows = res["rows"]
    assert len(rows) == 3 * 3  # symbols x horizons; one row per pair, none dropped
    by_key = {(r["symbol"], r["horizon_minutes"]): r for r in rows}
    assert by_key[("UP", 15)]["signal"] == "CALL"
    assert by_key[("DOWN", 15)]["signal"] == "PUT"
    assert by_key[("FLAT", 15)]["signal"] == "NO_TRADE"

    # every row carries provenance, freshness, drivers, and run status
    for r in rows:
        assert r["run_status"] == RUN_VALID
        assert r["provider"] == "yfinance" and r["market_state"] == "open"
        assert r["reference_time"] is not None and r["expiry_time"] is not None
        assert r["data_age_seconds"] is not None
    assert res["validation"]["overall_status"] == "ok"


def test_outputs_written_and_consistent(tmp_path):
    cfg = _cfg(tmp_path, ["UP", "DOWN"])
    provider = ScriptedProvider(_healthy_scripts(["UP", "DOWN"]))
    res = run_pipeline(cfg, provider=provider, now=NOW)

    csv_path = Path(cfg.run.processed_dir) / "binary_options_candidates.csv"
    md_path = Path(cfg.run.output_dir) / "binary_options_candidates.md"
    summary_path = Path(cfg.run.output_dir) / "binary_options_summary.json"
    diag_md = Path(cfg.run.output_dir) / "binary_options_provider_diagnostics.md"
    diag_json = Path(cfg.run.output_dir) / "binary_options_provider_diagnostics.json"
    val_md = Path(cfg.run.output_dir) / "binary_options_output_validation.md"
    val_json = Path(cfg.run.output_dir) / "binary_options_output_validation.json"
    for p in (csv_path, md_path, summary_path, diag_md, diag_json, val_md, val_json):
        assert p.exists(), p

    df = pd.read_csv(csv_path)
    assert len(df) == len(res["rows"])
    assert {"signal", "call_score", "put_score", "confidence_score",
            "no_trade_reason", "run_status"} <= set(df.columns)

    summary = json.loads(summary_path.read_text())
    assert summary["run_status"] == RUN_VALID
    assert summary["signal_counts"]["CALL"] >= 1
    assert "not calibrated probabilities" in summary["payout"]["note"]

    md = md_path.read_text()
    assert "How to read this report" in md
    assert "NO_TRADE" in md and "Disclaimer" in md
    assert "not a calibrated probability" in md


def test_partial_run_when_one_symbol_fails(tmp_path):
    symbols = ["UP", "DOWN", "FLAT", "S4", "S5", "S6", "S7", "GONE"]
    cfg = _cfg(tmp_path, symbols, horizons=(5, 15))
    scripts = _healthy_scripts(symbols[:-1])
    scripts["GONE"] = lambda: make_series("GONE", [], status="empty",
                                          error_type="NO_INTRADAY_DATA")
    res = run_pipeline(cfg, provider=ScriptedProvider(scripts), now=NOW)
    a = res["assessment"]
    assert a.run_status == RUN_PARTIAL, a.reasons
    gone_rows = [r for r in res["rows"] if r["symbol"] == "GONE"]
    assert len(gone_rows) == 2  # never silently dropped
    assert all(r["signal"] == "NO_TRADE" and r["no_trade_reason"] == "PROVIDER_COVERAGE"
               for r in gone_rows)


def test_degraded_run_when_many_rows_invalid(tmp_path):
    cfg = _cfg(tmp_path, ["UP", "GONE1", "GONE2"], horizons=(5, 15))
    scripts = _healthy_scripts(["UP"])
    for sym in ("GONE1", "GONE2"):
        scripts[sym] = lambda s=sym: make_series(s, [], status="empty",
                                                 error_type="NO_INTRADAY_DATA")
    res = run_pipeline(cfg, provider=ScriptedProvider(scripts), now=NOW)
    assert res["assessment"].run_status == RUN_DEGRADED


def test_invalid_run_on_systemic_provider_failure(tmp_path):
    cfg = _cfg(tmp_path, ["UP", "DOWN"])
    scripts = {
        sym: (lambda s=sym: make_series(s, [], status="error",
                                        error_type="PROVIDER_TIMEOUT"))
        for sym in ("UP", "DOWN", "SPY")
    }
    res = run_pipeline(cfg, provider=ScriptedProvider(scripts), now=NOW)
    a = res["assessment"]
    assert a.run_status == RUN_INVALID
    assert any("provider-side" in r for r in a.reasons)


def test_invalid_run_on_universe_error(tmp_path):
    cfg = _cfg(tmp_path, [])
    res = run_pipeline(cfg, provider=ScriptedProvider({}), now=NOW)
    assert res["assessment"].run_status == RUN_INVALID


def test_stale_data_produces_no_trade_not_fake_signal(tmp_path):
    from datetime import timedelta
    cfg = _cfg(tmp_path, ["UP"], horizons=(15,))
    old = NOW - timedelta(hours=2)
    provider = ScriptedProvider({
        "UP": lambda: make_series("UP", make_candles(step=uptrend, end=old)),
        "SPY": lambda: make_series("SPY", make_candles(step=flat, end=old)),
    })
    res = run_pipeline(cfg, provider=provider, now=NOW)
    row = res["rows"][0]
    assert row["signal"] == "NO_TRADE" and row["no_trade_reason"] == "STALE_DATA"
    assert row["signal_validity"] == "invalid"
    assert row["data_age_seconds"] > 3600


def test_benchmark_context_feeds_context_family(tmp_path):
    cfg = _cfg(tmp_path, ["UP"], horizons=(15,))
    provider = ScriptedProvider(_healthy_scripts(["UP"]))
    res = run_pipeline(cfg, provider=provider, now=NOW)
    row = res["rows"][0]
    assert "context" in row["family_contributions"]
    # benchmark missing -> context family missing, run degraded-or-valid but flagged
    provider2 = ScriptedProvider({
        "UP": lambda: make_series("UP", make_candles(step=uptrend)),
        "SPY": lambda: make_series("SPY", [], status="empty",
                                   error_type="NO_INTRADAY_DATA"),
    })
    res2 = run_pipeline(cfg, provider=provider2, now=NOW)
    row2 = res2["rows"][0]
    assert "context" in row2["missing_families"]
    assert res2["assessment"].run_status == RUN_DEGRADED


def test_sentiment_only_for_extended_horizons(tmp_path):
    cfg = _cfg(tmp_path, ["UP"], horizons=(15, 60))
    cfg.sentiment.enabled = True
    provider = ScriptedProvider(_healthy_scripts(["UP"]))
    # inject a sentiment score the way the asset-selection universe would
    from binary_options_selection import universe as uni
    orig = uni.resolve_universe
    def with_sentiment(*a, **k):
        r = orig(*a, **k)
        r.sentiment_by_symbol = {"UP": 80.0}
        return r
    import binary_options_selection.pipelines.run_binary_options_selection as pipe
    old = pipe.resolve_universe
    pipe.resolve_universe = with_sentiment
    try:
        res = run_pipeline(cfg, provider=provider, now=NOW)
    finally:
        pipe.resolve_universe = old
    by_h = {r["horizon_minutes"]: r for r in res["rows"]}
    assert "sentiment" in by_h[15]["missing_families"]          # short: excluded
    assert "sentiment" in by_h[60]["family_contributions"]      # extended: capped input


def test_payout_context_reported_but_no_edge(tmp_path):
    cfg = _cfg(tmp_path, ["UP"], horizons=(15,))
    cfg.payout.payout_ratio = 0.85
    res = run_pipeline(cfg, provider=ScriptedProvider(_healthy_scripts(["UP"])), now=NOW)
    row = res["rows"][0]
    assert row["payout_ratio"] == 0.85
    assert row["break_even_win_probability"] == pytest.approx(1 / 1.85, abs=1e-4)
    assert row["calibrated_probability"] is None
    assert row["expected_edge"] is None
    assert res["validation"]["overall_status"] == "ok"


def test_output_validator_catches_fabricated_probability():
    rows = [{"symbol": "X", "horizon_minutes": 15, "signal": "NO_TRADE",
             "no_trade_reason": "STALE_DATA", "expected_edge": 0.12}]
    result = validate_outputs(rows, run_status="VALID")
    assert result["overall_status"] == "error"
    names = {c["name"]: c["status"] for c in result["checks"]}
    assert names["no_fabricated_probability"] == "error"


def test_market_closed_produces_no_trade(tmp_path):
    from datetime import datetime, timezone
    saturday = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
    cfg = _cfg(tmp_path, ["UP"], horizons=(15,))
    provider = ScriptedProvider({
        "UP": lambda: make_series("UP", make_candles(step=uptrend, end=saturday)),
        "SPY": lambda: make_series("SPY", make_candles(step=flat, end=saturday)),
    })
    res = run_pipeline(cfg, provider=provider, now=saturday)
    row = res["rows"][0]
    assert row["signal"] == "NO_TRADE"
    assert row["no_trade_reason"] == "MARKET_CLOSED"
    assert row["market_state"] == "closed"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_rejects_bad_horizons_with_exit_2(capsys):
    assert main(["--horizons", "3", "121"]) == 2
    err = capsys.readouterr().err
    assert "outside the supported range" in err


def test_cli_rejects_unknown_provider(capsys):
    assert main(["--provider", "broker_x"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_cli_parser_has_only_implemented_flags():
    parser = build_arg_parser()
    flags = {a.option_strings[0] for a in parser._actions if a.option_strings}
    assert flags == {
        "-h", "--config", "--tickers", "--watchlist", "--universe",
        "--asset-selection-input", "--horizons", "--interval", "--top",
        "--provider", "--refresh-cache", "--no-cache", "--output-dir",
        "--log-level", "--health-check-only", "--allow-extended-hours",
        "--allow-partial-results", "--no-partial-results",
    }


def test_watchlist_file_mode(tmp_path):
    wl = tmp_path / "watch.txt"
    wl.write_text("UP\n# comment\nDOWN\n")
    cfg = _cfg(tmp_path, [])
    cfg.universe.mode = "watchlist"
    from binary_options_selection.universe import resolve_universe
    r = resolve_universe(cfg, cli_watchlist_file=str(wl))
    assert r.symbols == ["UP", "DOWN"] and not r.error


def test_asset_selection_universe_is_filter_only(tmp_path):
    df = pd.DataFrame({
        "ticker": ["AAA", "BBB", "CCC"],
        "final_score": [90, 80, 70],
        "sentiment_score": [75.0, 40.0, 55.0],
        "company_name": ["A Corp", "B Inc", "C Ltd"],
    })
    path = tmp_path / "asset_selection_results.csv"
    df.to_csv(path, index=False)
    cfg = make_config()
    cfg.universe.mode = "asset-selection"
    cfg.universe.asset_selection_input = str(path)
    cfg.universe.asset_selection_top_k = 2
    from binary_options_selection.universe import resolve_universe
    r = resolve_universe(cfg)
    assert r.symbols == ["AAA", "BBB"]                      # top-k filter only
    assert r.sentiment_by_symbol == {"AAA": 75.0, "BBB": 40.0}
    assert any("not an intraday predictor" in n for n in r.notes)


# ---------------------------------------------------------------------------
# backward compatibility with the equity module
# ---------------------------------------------------------------------------

def test_asset_selection_cli_still_importable():
    from asset_selection.pipelines.run_asset_selection import main as eq_main
    assert callable(eq_main)


def test_shared_utils_shims_still_work():
    from asset_selection.utils.cache import Cache as C1
    from trading_common.cache import Cache as C2
    assert C1 is C2
    from asset_selection.data_providers import errors as eq_err
    assert eq_err.PROVIDER_RATE_LIMITED == "PROVIDER_RATE_LIMITED"
    assert eq_err.classify_exception(TimeoutError("timed out")) == "PROVIDER_TIMEOUT"
