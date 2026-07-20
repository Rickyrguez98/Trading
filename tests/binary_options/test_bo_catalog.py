"""Instrument catalog: providers, validation, and contract gates."""
from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

from binary_options_selection.catalog import (
    BinaryInstrument,
    CatalogError,
    CatalogSnapshot,
    build_catalog_provider,
    validate_catalog,
)
from binary_options_selection.catalog.context import (
    build_contract_context,
    unknown_contract_context,
)
from binary_options_selection.config import load_config
from binary_options_selection.market_session import SessionState, expiry_for
from binary_options_selection.scoring.directional import score_direction
from binary_options_selection.signals.decision import decide_signal
from binary_options_selection.features.engine import compute_features
from binary_options_selection.signals.decision import assess_liquidity
from binary_options_selection.validation.candles import assess_series

from tests.binary_options.bo_fixtures import (
    NOW,
    make_candles,
    make_config,
    make_series,
    trend_volume,
    uptrend,
)


def _catalog_dict(**inst_overrides):
    inst = {
        "platform_asset_id": "T-AAPL",
        "underlying_symbol": "AAPL",
        "provider_symbol": "AAPL",
        "asset_type": "equity",
        "available_now": True,
        "contract_status": "open",
        "available_expirations_minutes": [5, 10, 15, 30, 60],
        "payout_by_expiration": {5: 0.8, 15: 0.85, 30: 0.85, 60: 0.87},
    }
    inst.update(inst_overrides)
    return {
        "platform": "test_platform",
        "catalog_timestamp": NOW.isoformat(),
        "instruments": [inst],
    }


def _write_yaml(tmp_path, data, name="cat.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _load(tmp_path, data):
    return build_catalog_provider(_write_yaml(tmp_path, data)).get_available_instruments()


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------

def test_yaml_catalog_loads(tmp_path):
    snap = _load(tmp_path, _catalog_dict())
    assert snap.provider == "yaml" and snap.platform == "test_platform"
    inst = snap.instruments[0]
    assert inst.supports_expiration(30) and not inst.supports_expiration(90)
    assert inst.payout_for(15) == 0.85 and inst.payout_for(90) is None
    assert snap.catalog_hash and snap.age_seconds(NOW) == 0.0


def test_csv_catalog_loads(tmp_path):
    p = tmp_path / "cat.csv"
    p.write_text(
        "platform,platform_asset_id,underlying_symbol,provider_symbol,asset_type,"
        "available_now,contract_status,available_expirations_minutes,"
        "payout_by_expiration,is_otc,catalog_timestamp\n"
        f"test_platform,T-EURUSD,EURUSD,EURUSD=X,forex,true,open,5|10|15,"
        f"5:0.8|10:0.82|15:0.84,false,{NOW.isoformat()}\n"
    )
    snap = build_catalog_provider(str(p)).get_available_instruments()
    inst = snap.instruments[0]
    assert inst.asset_type == "forex" and inst.provider_symbol == "EURUSD=X"
    assert inst.available_expirations_minutes == [5, 10, 15]
    assert inst.payout_for(10) == 0.82
    assert snap.catalog_timestamp is not None


def test_missing_and_invalid_files(tmp_path):
    with pytest.raises(CatalogError):
        build_catalog_provider(str(tmp_path / "none.yaml")).get_available_instruments()
    bad = tmp_path / "bad.yaml"
    bad.write_text("]:[ not yaml")
    with pytest.raises(CatalogError):
        build_catalog_provider(str(bad)).get_available_instruments()


def test_platform_filter(tmp_path):
    data = _catalog_dict()
    data["instruments"].append({**_catalog_dict()["instruments"][0],
                                "platform": "other", "platform_asset_id": "O-1"})
    snap = build_catalog_provider(_write_yaml(tmp_path, data)).get_available_instruments(
        platform="other"
    )
    assert [i.platform_asset_id for i in snap.instruments] == ["O-1"]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _validate(tmp_path, data, *, now=NOW):
    cfg = make_config()
    return validate_catalog(_load(tmp_path, data), cfg.instrument_catalog, now=now)


def test_valid_catalog_ok(tmp_path):
    r = _validate(tmp_path, _catalog_dict())
    assert r.ok and not r.stale and not r.instrument_issues


def test_missing_timestamp_is_fatal(tmp_path):
    data = _catalog_dict()
    del data["catalog_timestamp"]
    r = _validate(tmp_path, data)
    assert not r.ok and any("catalog_timestamp" in p for p in r.problems)


def test_stale_catalog_flagged(tmp_path):
    r = _validate(tmp_path, _catalog_dict(), now=NOW + timedelta(hours=2))
    assert r.ok and r.stale  # usable schema, but freshness gate will fire


def test_duplicate_platform_ids_fatal(tmp_path):
    data = _catalog_dict()
    data["instruments"].append(dict(data["instruments"][0]))
    r = _validate(tmp_path, data)
    assert not r.ok and any("duplicate" in p for p in r.problems)


def test_instrument_issues_reported(tmp_path):
    data = _catalog_dict(
        asset_type="warrant",                       # unsupported
        available_expirations_minutes=[3, 15],      # 3m out of range
        payout_by_expiration={15: 9.0, 90: 0.5},    # implausible + unavailable expiry
    )
    r = _validate(tmp_path, data)
    issues = " | ".join(r.instrument_issues["T-AAPL"])
    assert "unsupported asset_type" in issues
    assert "outside the supported 5-120" in issues
    assert "implausible payout" in issues
    assert "unavailable expiration" in issues


def test_missing_symbol_mapping_reported(tmp_path):
    data = _catalog_dict(underlying_symbol="", provider_symbol="")
    r = _validate(tmp_path, data)
    assert any("underlying_symbol" in i for i in r.instrument_issues["T-AAPL"])


def test_missing_payout_warns(tmp_path):
    data = _catalog_dict(payout_by_expiration={})
    r = _validate(tmp_path, data)
    assert r.ok and any("no payout data" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# contract context + decision gates
# ---------------------------------------------------------------------------

CFG = make_config()
SESSION = SessionState("open", None, "America/New_York", True)


def _decide_with(instrument, horizon=15, *, catalog_stale=False, research=False,
                 market_price=None, now=NOW):
    candles = make_candles(step=uptrend, volume=trend_volume)
    quality = assess_series(make_series("AAPL", candles), CFG, now)
    bundle = compute_features(quality.candles, horizon_minutes=horizon,
                              interval_minutes=5, cfg=CFG)
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score,
                             cfg=CFG)
    snap = CatalogSnapshot(
        provider="yaml", platform="test_platform", path=None,
        catalog_timestamp=now, loaded_at=now, instruments=[instrument],
    )
    ctx = build_contract_context(
        instrument, snapshot=snap, catalog_stale=catalog_stale,
        horizon_minutes=horizon, cfg=CFG.instrument_catalog,
        research_mode=research,
        market_reference_price=market_price if market_price is not None
        else quality.reference_price,
        now=now,
    )
    return decide_signal(
        scores, quality, SESSION, assess_liquidity(quality.candles, CFG),
        expiry_utc=expiry_for(quality.latest_complete_close, horizon), cfg=CFG,
        contract=ctx,
    ), ctx


def _instrument(**over):
    base = dict(
        platform="test_platform", platform_asset_id="T-AAPL",
        underlying_symbol="AAPL", asset_type="equity",
        available_expirations_minutes=[5, 10, 15, 30, 60],
        payout_by_expiration={15: 0.85},
    )
    base.update(over)
    return BinaryInstrument(**base)


def test_available_contract_is_actionable():
    d, ctx = _decide_with(_instrument())
    assert d.signal == "CALL" and d.recommended_action == "BUY_CALL"
    assert ctx.payout_ratio == 0.85


def test_unavailable_expiration_gate():
    d, _ = _decide_with(_instrument(), horizon=90)
    assert d.no_trade_reason == "EXPIRATION_NOT_AVAILABLE"


def test_contract_closed_gate():
    d, _ = _decide_with(_instrument(contract_status="closed"))
    assert d.no_trade_reason == "CONTRACT_CLOSED"


def test_instrument_not_available_gate():
    d, _ = _decide_with(_instrument(available_now=False))
    assert d.no_trade_reason == "INSTRUMENT_NOT_AVAILABLE"


def test_otc_excluded_by_default_and_warned_when_allowed():
    d, _ = _decide_with(_instrument(is_otc=True))
    assert d.no_trade_reason == "OTC_EXCLUDED"
    cfg = make_config()
    cfg.instrument_catalog.exclude_otc = False
    inst = _instrument(is_otc=True)
    candles = make_candles(step=uptrend, volume=trend_volume)
    quality = assess_series(make_series("AAPL", candles), cfg, NOW)
    bundle = compute_features(quality.candles, horizon_minutes=15,
                              interval_minutes=5, cfg=cfg)
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score,
                             cfg=cfg)
    snap = CatalogSnapshot(provider="yaml", platform="p", path=None,
                           catalog_timestamp=NOW, loaded_at=NOW, instruments=[inst])
    ctx = build_contract_context(
        inst, snapshot=snap, catalog_stale=False, horizon_minutes=15,
        cfg=cfg.instrument_catalog, research_mode=False,
        market_reference_price=quality.reference_price, now=NOW,
    )
    d2 = decide_signal(
        scores, quality, SESSION, assess_liquidity(quality.candles, cfg),
        expiry_utc=expiry_for(quality.latest_complete_close, 15), cfg=cfg,
        contract=ctx,
    )
    assert d2.signal == "CALL" and "OTC_CONTRACT" in d2.warning_flags


def test_catalog_stale_gate_and_research_softening():
    d, _ = _decide_with(_instrument(), catalog_stale=True)
    assert d.no_trade_reason == "CATALOG_STALE"
    d2, _ = _decide_with(_instrument(), catalog_stale=True, research=True)
    assert d2.signal == "CALL" and "CATALOG_STALE" in d2.warning_flags


def test_payout_gates():
    d, _ = _decide_with(_instrument(payout_by_expiration={}))
    assert d.no_trade_reason == "PAYOUT_UNKNOWN"
    d2, _ = _decide_with(_instrument(payout_by_expiration={}), research=True)
    assert d2.signal == "CALL" and "PAYOUT_UNKNOWN" in d2.warning_flags
    cfg_min = make_config()
    cfg_min.instrument_catalog.minimum_acceptable_payout = 0.9
    inst = _instrument()
    candles = make_candles(step=uptrend, volume=trend_volume)
    quality = assess_series(make_series("AAPL", candles), cfg_min, NOW)
    snap = CatalogSnapshot(provider="yaml", platform="p", path=None,
                           catalog_timestamp=NOW, loaded_at=NOW, instruments=[inst])
    ctx = build_contract_context(
        inst, snapshot=snap, catalog_stale=False, horizon_minutes=15,
        cfg=cfg_min.instrument_catalog, research_mode=False,
        market_reference_price=quality.reference_price, now=NOW,
    )
    assert ctx.payout_below_minimum


def test_quote_mismatch_and_stale_quote():
    # platform quote far from market reference -> mismatch
    inst = _instrument(platform_reference_price=250.0,
                       reference_price_timestamp=NOW)
    d, ctx = _decide_with(inst, market_price=100.0)
    assert ctx.quote_alignment_status == "mismatch"
    assert d.no_trade_reason == "QUOTE_MISMATCH"
    assert ctx.relative_quote_difference_bps > 50
    # old platform quote -> stale
    inst2 = _instrument(platform_reference_price=100.0,
                        reference_price_timestamp=NOW - timedelta(hours=1))
    d2, ctx2 = _decide_with(inst2, market_price=100.0)
    assert ctx2.quote_alignment_status == "stale"
    assert d2.no_trade_reason == "PLATFORM_QUOTE_STALE"


def test_aligned_quote_passes():
    inst = _instrument(platform_reference_price=None)
    _, ctx = _decide_with(inst)
    assert ctx.quote_alignment_status == "unavailable"
    # near-identical quote aligns (uptrend reference ~ close of last candle)
    candles = make_candles(step=uptrend, volume=trend_volume)
    ref = candles[-1].close
    inst2 = _instrument(platform_reference_price=ref * 1.0001,
                        reference_price_timestamp=NOW)
    d, ctx2 = _decide_with(inst2, market_price=ref)
    assert ctx2.quote_alignment_status == "aligned" and d.signal == "CALL"


def test_unknown_context_defaults_to_availability_unknown():
    ctx = unknown_contract_context(research_mode=False)
    candles = make_candles(step=uptrend, volume=trend_volume)
    quality = assess_series(make_series("AAPL", candles), CFG, NOW)
    bundle = compute_features(quality.candles, horizon_minutes=15,
                              interval_minutes=5, cfg=CFG)
    scores = score_direction(bundle, data_quality_score=quality.data_quality_score,
                             cfg=CFG)
    d = decide_signal(
        scores, quality, SESSION, assess_liquidity(quality.candles, CFG),
        expiry_utc=expiry_for(quality.latest_complete_close, 15), cfg=CFG,
        contract=ctx,
    )
    assert d.no_trade_reason == "INSTRUMENT_AVAILABILITY_UNKNOWN"


def test_config_rejects_bad_catalog_settings():
    from binary_options_selection.config import ConfigValidationError
    with pytest.raises(ConfigValidationError):
        load_config({"instrument_catalog": {"provider": "sqlite"}})
    with pytest.raises(ConfigValidationError):
        load_config({"instrument_catalog": {"asset_types": ["warrant"]}})
    with pytest.raises(ConfigValidationError):
        load_config({"instrument_catalog": {"max_quote_diff_bps": -1}})
