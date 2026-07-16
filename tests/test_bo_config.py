"""Configuration parsing and semantic validation for binary_options_selection."""
from __future__ import annotations

import pytest

from binary_options_selection.config import (
    ConfigValidationError,
    HORIZON_GROUP_EXTENDED,
    HORIZON_GROUP_SHORT,
    horizon_group,
    load_config,
    validate_config,
)


def test_default_yaml_loads_and_validates():
    cfg = load_config("configs/binary_options_config.yaml")
    assert cfg.horizons.minutes == [5, 10, 15, 30, 60, 90, 120]
    assert cfg.candles.interval == "5m"
    assert validate_config(cfg) == []


def test_dataclass_defaults_are_valid():
    assert validate_config(load_config({})) == []


def test_horizon_groups():
    assert horizon_group(5) == HORIZON_GROUP_SHORT
    assert horizon_group(30) == HORIZON_GROUP_SHORT      # 30 is inclusive-short
    assert horizon_group(31) == HORIZON_GROUP_EXTENDED
    assert horizon_group(120) == HORIZON_GROUP_EXTENDED


def test_unknown_yaml_keys_are_tolerated():
    cfg = load_config({"run": {"top_n": 7, "not_a_real_key": True}})
    assert cfg.run.top_n == 7


@pytest.mark.parametrize("raw,fragment", [
    ({"candles": {"interval": "3m"}}, "unsupported"),
    ({"horizons": {"minutes": [4]}}, "outside the supported range"),
    ({"horizons": {"minutes": [121]}}, "outside the supported range"),
    ({"horizons": {"minutes": [17]}}, "whole multiple"),
    ({"horizons": {"minutes": [15, 15]}}, "duplicates"),
    ({"horizons": {"minutes": []}}, "must not be empty"),
    ({"candles": {"interval": "15m"}, "horizons": {"minutes": [5]}}, "shorter than the candle interval"),
    ({"features": {"rsi_period_bars": -3}}, "positive integer"),
    ({"features": {"ema_fast_bars": 30}}, "ema_fast_bars must be < ema_slow_bars"),
    ({"signal_rules": {"min_confidence": 1.5}}, "within [0, 1]"),
    ({"signal_rules": {"min_directional_margin": 250}}, "within [0, 100]"),
    ({"payout": {"payout_ratio": -0.5}}, "payout_ratio"),
    ({"payout": {"payout_ratio": 9.0}}, "payout_ratio"),
    ({"providers": {"intraday": "made_up_broker"}}, "not implemented"),
    ({"universe": {"mode": "everything"}}, "universe.mode"),
    ({"market_session": {"session": "regular", "allow_premarket": True}}, "extended"),
    ({"market_session": {"regular_open": "25:99"}}, "not HH:MM"),
])
def test_invalid_configs_are_rejected(raw, fragment):
    with pytest.raises(ConfigValidationError) as exc:
        load_config(raw)
    assert fragment in str(exc.value)


def test_all_problems_reported_at_once():
    with pytest.raises(ConfigValidationError) as exc:
        load_config({
            "candles": {"interval": "7m"},
            "horizons": {"minutes": [3, 200]},
            "payout": {"payout_ratio": -1},
        })
    assert len(exc.value.problems) >= 4


def test_weights_single_family_dominance_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        load_config({"weights": {"short": {"momentum": 0.9, "trend": 0.1}}})
    assert "no single family may exceed" in str(exc.value)


def test_weights_sentiment_caps_by_group():
    # 10% sentiment is fine for extended horizons but rejected for short ones.
    ok = {"weights": {"extended_intraday": {
        "momentum": 0.3, "trend": 0.3, "volume": 0.3, "sentiment": 0.1}}}
    load_config(ok)
    bad = {"weights": {"short": {
        "momentum": 0.3, "trend": 0.3, "volume": 0.3, "sentiment": 0.1}}}
    with pytest.raises(ConfigValidationError) as exc:
        load_config(bad)
    assert "sentiment" in str(exc.value)


def test_weights_negative_and_unknown_rejected():
    with pytest.raises(ConfigValidationError) as exc:
        load_config({"weights": {"short": {"momentum": -0.2, "astrology": 0.5}}})
    msg = str(exc.value)
    assert "unknown families" in msg and ">= 0" in msg


def test_missing_explicit_config_path_raises():
    with pytest.raises(FileNotFoundError):
        load_config("configs/does_not_exist.yaml")


def test_interval_aware_cache_ttl():
    cfg = load_config({})
    # 5m interval, fraction 0.5 -> 150s, within [15, 900]
    assert cfg.cache.ttl_for_interval(300) == 150
    assert cfg.cache.ttl_for_interval(10) == 15         # floor
    assert cfg.cache.ttl_for_interval(7200) == 900      # ceiling
