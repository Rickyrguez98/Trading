"""Cross-domain architecture guarantees.

US Equities and Binary Options must stay separated in configuration, data,
reports, and cache paths; shared modules must stay generic; legacy paths must
keep working through documented compatibility shims.
"""
from __future__ import annotations

import inspect
import warnings
from pathlib import Path

def _equity_cfg():
    from asset_selection.config import load_config

    return load_config("configs/us_equities/default.yaml")


def _binary_cfg():
    from binary_options_selection.config import load_config

    return load_config("configs/binary_options/default.yaml")


def test_output_and_cache_paths_do_not_collide():
    eq, bo = _equity_cfg(), _binary_cfg()
    eq_paths = {eq.run.output_dir, eq.run.processed_dir, eq.cache.dir}
    bo_paths = {bo.run.output_dir, bo.run.processed_dir, bo.cache.dir,
                bo.run.input_dir}
    assert not (eq_paths & bo_paths)
    assert all(p.startswith(("reports/us_equities", "data/us_equities")) for p in eq_paths)
    assert all(p.startswith(("reports/binary_options", "data/binary_options"))
               for p in bo_paths)


def test_domain_configs_load_from_domain_directories():
    assert _equity_cfg().run.output_dir == "reports/us_equities"
    assert _binary_cfg().run.output_dir == "reports/binary_options"
    assert Path("configs/us_equities/default.yaml").exists()
    assert Path("configs/binary_options/default.yaml").exists()
    assert Path("configs/binary_options/instruments.example.yaml").exists()


def test_flat_config_defaults_migrate_to_domain_paths():
    # Old YAMLs that still say `reports` / `data/processed` are migrated.
    from asset_selection.config import load_config as eq_load
    from binary_options_selection.config import load_config as bo_load

    eq = eq_load({"run": {"output_dir": "reports", "processed_dir": "data/processed"}})
    assert eq.run.output_dir == "reports/us_equities"
    assert eq.run.processed_dir == "data/us_equities/processed"
    bo = bo_load({"run": {"output_dir": "reports", "processed_dir": "data/processed"}})
    assert bo.run.output_dir == "reports/binary_options"
    assert bo.run.processed_dir == "data/binary_options/processed"


def test_shared_modules_contain_no_domain_imports():
    import trading_common.cache
    import trading_common.errors
    import trading_common.io
    import trading_common.rate_limiter

    for mod in (trading_common.cache, trading_common.errors,
                trading_common.io, trading_common.rate_limiter):
        src = inspect.getsource(mod)
        assert "asset_selection" not in src, mod.__name__
        assert "binary_options" not in src, mod.__name__


def test_legacy_flat_config_path_still_resolves_with_warning(tmp_path, monkeypatch):
    import shutil

    monkeypatch.chdir(tmp_path)
    src = Path(__file__).resolve().parent.parent.parent / "configs"
    (tmp_path / "configs").mkdir()
    shutil.copy(src / "us_equities" / "default.yaml",
                tmp_path / "configs" / "default_config.yaml")
    from asset_selection.config import load_config

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = load_config(None)  # no explicit path -> legacy fallback
    assert cfg.run.top_n is not None
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_legacy_module_commands_still_importable():
    from asset_selection.pipelines.run_asset_selection import main as eq_main
    from binary_options_selection.pipelines.run_binary_options_selection import (
        main as bo_main,
    )

    assert callable(eq_main) and callable(bo_main)


def test_pyproject_registers_domain_clis():
    text = Path("pyproject.toml").read_text()
    assert 'trading-us-equities = "asset_selection.pipelines.run_asset_selection:main"' in text
    assert ('trading-binary-options = '
            '"binary_options_selection.pipelines.run_binary_options_selection:main"') in text
