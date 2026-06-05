"""Config loading."""
from __future__ import annotations

from pathlib import Path

import yaml

from asset_selection.config import AppConfig, load_config


def test_load_default_config_from_disk():
    cfg = load_config("configs/default_config.yaml")
    assert isinstance(cfg, AppConfig)
    assert cfg.run.top_n > 0
    assert cfg.composite.weights["fundamentals"] >= cfg.composite.weights["sentiment"], (
        "By default, fundamentals must outweigh sentiment."
    )
    assert "growth" in cfg.scoring.growth or "revenue_growth" in cfg.scoring.growth


def test_load_config_from_dict_overrides_defaults():
    cfg = load_config({"run": {"top_n": 7, "max_tickers": 3}})
    assert cfg.run.top_n == 7
    assert cfg.run.max_tickers == 3


def test_load_config_ignores_unknown_yaml_keys(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump({
        "run": {"top_n": 11, "future_field": "ignored"},
        "totally_new_section": {"hello": 1},
    }))
    cfg = load_config(p)
    assert cfg.run.top_n == 11
