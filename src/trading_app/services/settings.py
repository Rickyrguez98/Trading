"""App preferences, stored in ``data/app/settings.json`` (git-ignored).

Only non-secret choices live here. IBKR credentials are never accepted by the
app; option C reads them from the environment (``.env``), exactly as the CLI
does.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional

from asset_selection.monitoring.config import load_config as load_monitoring_config

from .paths import Paths


@dataclass
class AppSettings:
    broker_option: str = "B"
    holdings_csv: str = "data/us_equities/holdings/my_holdings.csv"
    statement_pdf: Optional[str] = None


def load_settings(paths: Paths) -> AppSettings:
    """Saved settings, else the monitoring config's broker section."""
    if paths.settings_json.exists():
        try:
            data = json.loads(paths.settings_json.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__}
            return AppSettings(**known)
        except (OSError, ValueError, TypeError):
            pass
    try:
        b = load_monitoring_config(paths.monitoring_config).broker
        from asset_selection.monitoring.broker import resolve_option

        return AppSettings(broker_option=resolve_option(b.option).code, holdings_csv=b.holdings_csv,
                           statement_pdf=b.statement_pdf)
    except Exception:  # noqa: BLE001 - fall back to defaults
        return AppSettings()


def save_settings(paths: Paths, settings: AppSettings) -> None:
    if settings.broker_option not in ("A", "B", "C"):
        raise ValueError("broker option must be A, B or C")
    paths.app_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.settings_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    tmp.replace(paths.settings_json)
