"""End-to-end contract-aware pipeline behavior (offline, scripted provider)."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import yaml

from binary_options_selection.pipelines.run_binary_options_selection import (
    main,
    run_pipeline,
)
from binary_options_selection.utils.timeutils import iso_utc

from tests.binary_options.bo_fixtures import (
    NOW,
    ScriptedProvider,
    flat,
    make_candles,
    make_config,
    make_series,
    trend_volume,
    uptrend,
)


def _catalog_file(tmp_path, *, stamp=None, instruments=None):
    data = {
        "platform": "test_platform",
        "catalog_timestamp": iso_utc(stamp or NOW),
        "instruments": instruments if instruments is not None else [
            {"platform_asset_id": "T-AAPL", "underlying_symbol": "AAPL",
             "provider_symbol": "AAPL", "asset_type": "equity",
             "available_now": True, "contract_status": "open",
             "available_expirations_minutes": [5, 10, 15, 30, 60],
             "payout_by_expiration": {5: 0.8, 10: 0.8, 15: 0.85, 30: 0.85, 60: 0.87}},
            {"platform_asset_id": "T-EURUSD", "underlying_symbol": "EURUSD",
             "provider_symbol": "EURUSD=X", "asset_type": "forex",
             "available_now": True, "contract_status": "open",
             "available_expirations_minutes": [5, 15, 30, 60, 90, 120],
             "payout_by_expiration": {5: 0.8, 15: 0.82, 30: 0.84, 60: 0.85,
                                      90: 0.85, 120: 0.85}},
        ],
    }
    p = tmp_path / "instruments.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _cfg(tmp_path, horizons=(5, 15, 30, 60, 90)):
    cfg = make_config()
    cfg.universe.mode = "catalog"
    cfg.horizons.minutes = list(horizons)
    cfg.run.output_dir = str(tmp_path / "reports")
    cfg.run.processed_dir = str(tmp_path / "processed")
    return cfg


def _provider():
    return ScriptedProvider({
        "AAPL": lambda: make_series("AAPL", make_candles(step=uptrend, volume=trend_volume)),
        "EURUSD": lambda: make_series("EURUSD", make_candles(step=uptrend,
                                                             volume=lambda i: 0.0)),
        "SPY": lambda: make_series("SPY", make_candles(step=flat)),
    })


def test_catalog_mode_full_matrix(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.instrument_catalog.path = _catalog_file(tmp_path)
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    rows = {(r["underlying_symbol"], r["horizon_minutes"]): r for r in res["rows"]}

    # Every instrument x requested horizon appears -- including unavailable ones.
    assert len(res["rows"]) == 2 * 5
    assert rows[("AAPL", 90)]["signal"] == "NO_TRADE"
    assert rows[("AAPL", 90)]["no_trade_reason"] == "EXPIRATION_NOT_AVAILABLE"

    # Actionable rows carry the full contract context.
    a15 = rows[("AAPL", 15)]
    assert a15["signal"] == "CALL" and a15["recommended_action"] == "BUY_CALL"
    assert a15["platform"] == "test_platform"
    assert a15["platform_asset_id"] == "T-AAPL"
    assert a15["contract_available"] is True and a15["availability_known"] is True
    assert a15["payout_ratio"] == 0.85
    assert abs(a15["break_even_win_probability"] - 1 / 1.85) < 1e-3
    assert a15["catalog_provider"] == "yaml"
    assert a15["catalog_age_seconds"] is not None
    assert a15["ranking_validity"] == "actionable"
    assert a15["horizon_group"] == "short_term"
    assert rows[("AAPL", 30)]["horizon_group"] == "extended_intraday"

    # Forex: volume gates skipped with a warning, session is 24/5.
    e15 = rows[("EURUSD", 15)]
    assert e15["asset_type"] == "forex"
    assert e15["market_state"] == "open_24_5"
    assert "VOLUME_DATA_UNRELIABLE" in e15["warning_flags"]
    assert e15["signal"] in ("CALL", "PUT", "NO_TRADE")


def test_no_catalog_means_availability_unknown(tmp_path):
    cfg = _cfg(tmp_path, horizons=(15,))
    assert cfg.instrument_catalog.path is None
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    assert res["rows"], "watchlist symbols still get explainable rows"
    for r in res["rows"]:
        assert r["signal"] == "NO_TRADE"
        assert r["no_trade_reason"] == "INSTRUMENT_AVAILABILITY_UNKNOWN"
        assert r["availability_known"] is False
    assert res["assessment"].run_status in ("DEGRADED", "PARTIAL")


def test_stale_catalog_gates_all_rows(tmp_path):
    cfg = _cfg(tmp_path, horizons=(15, 60))
    cfg.instrument_catalog.path = _catalog_file(
        tmp_path, stamp=NOW - timedelta(hours=3)
    )
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    for r in res["rows"]:
        assert r["signal"] == "NO_TRADE"
        assert r["no_trade_reason"] == "CATALOG_STALE"


def test_known_provider_delay_gates_short_term_only(tmp_path):
    cfg = _cfg(tmp_path, horizons=(5, 15, 60))
    cfg.instrument_catalog.path = _catalog_file(tmp_path)
    cfg.providers.known_delay_seconds = {"yfinance": 900.0}
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    rows = {(r["underlying_symbol"], r["horizon_minutes"]): r for r in res["rows"]}
    assert rows[("AAPL", 5)]["no_trade_reason"] == "DATA_NOT_REAL_TIME"
    assert rows[("AAPL", 15)]["no_trade_reason"] == "DATA_NOT_REAL_TIME"
    assert rows[("AAPL", 60)]["signal"] == "CALL"
    assert rows[("AAPL", 5)]["known_provider_delay_seconds"] == 900.0


def test_actionable_rows_never_include_unavailable_contracts(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.instrument_catalog.path = _catalog_file(tmp_path)
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    for r in res["rows"]:
        if r["ranking_validity"] == "actionable":
            assert r["contract_available"] and r["availability_known"]
            assert r["signal"] in ("CALL", "PUT")
        else:
            assert r["signal"] == "NO_TRADE"
    # ranked block comes first, NO_TRADE afterwards (separation preserved)
    seen_no_trade = False
    for r in res["rows"]:
        if r["signal"] == "NO_TRADE":
            seen_no_trade = True
        else:
            assert not seen_no_trade


def test_catalog_validation_failure_invalidates_run(tmp_path):
    # duplicate platform ids -> fatal validation problem -> INVALID run
    inst = {"platform_asset_id": "DUP", "underlying_symbol": "AAPL",
            "asset_type": "equity", "available_expirations_minutes": [15],
            "payout_by_expiration": {15: 0.8}}
    cfg = _cfg(tmp_path, horizons=(15,))
    cfg.instrument_catalog.path = _catalog_file(
        tmp_path, instruments=[inst, dict(inst)]
    )
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    assert res["assessment"].run_status == "INVALID"


def test_cli_requires_research_mode_for_generic_symbols(tmp_path, capsys):
    assert main(["--tickers", "AAPL", "MSFT"]) == 2
    err = capsys.readouterr().err
    assert "research_mode" in err or "--research-mode" in err


def test_cli_validate_catalog_only(tmp_path, capsys):
    path = _catalog_file(tmp_path)
    assert main(["--validate-catalog-only", "--instrument-catalog", path]) == 0
    out = capsys.readouterr().out
    assert "Catalog schema: OK" in out
    # missing file -> actionable error, exit 2
    assert main(["--validate-catalog-only",
                 "--instrument-catalog", str(tmp_path / "nope.yaml")]) == 2


def test_run_summary_records_catalog_and_matrix(tmp_path):
    cfg = _cfg(tmp_path, horizons=(15, 90))
    cfg.instrument_catalog.path = _catalog_file(tmp_path)
    res = run_pipeline(cfg, provider=_provider(), now=NOW)
    run_dir = Path(cfg.run.output_dir) / "runs" / res["run_id"]
    summary = json.loads((run_dir / "run_summary.json").read_text())
    assert summary["catalog"]["platform"] == "test_platform"
    assert summary["catalog"]["instruments"] == 2
    assert set(summary["horizon_matrix"]["AAPL"]) == {"15m", "90m"}
    assert summary["horizon_matrix"]["AAPL"]["90m"] == "NO_TRADE"
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    assert manifest["catalog_path"] == cfg.instrument_catalog.path
    assert manifest["catalog_hash"]
