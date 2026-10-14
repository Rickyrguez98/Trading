"""Where every pipeline reads and writes, in one place.

The defaults mirror the pipelines' own configuration so the app and the CLIs
always look at the same files. App-only state (settings, jobs, uploads) lives
under ``data/app``, which git ignores.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

STRATEGIES = ("conservative", "balanced", "aggressive")


@dataclass(frozen=True)
class Paths:
    root: Path

    # Step 1 -- selection
    @property
    def selection_config(self) -> Path:
        return self.root / "configs/us_equities/default.yaml"

    @property
    def ranking_csv(self) -> Path:
        return self.root / "data/us_equities/processed/asset_selection_results.csv"

    @property
    def selection_summary(self) -> Path:
        return self.root / "reports/us_equities/asset_selection_summary.json"

    # Step 2 -- allocation
    @property
    def portfolio_config(self) -> Path:
        return self.root / "configs/us_equities/portfolio.yaml"

    def plan_csv(self, strategy: str) -> Path:
        return self.root / f"data/us_equities/processed/portfolio_{strategy}.csv"

    def plans(self) -> Dict[str, Path]:
        return {s: self.plan_csv(s) for s in STRATEGIES if self.plan_csv(s).exists()}

    @property
    def allocation_summary(self) -> Path:
        return self.root / "reports/us_equities/portfolio/portfolio_summary.json"

    # Step 4 -- monitor
    @property
    def monitoring_config(self) -> Path:
        return self.root / "configs/us_equities/monitoring.yaml"

    @property
    def monitor_summary(self) -> Path:
        return self.root / "reports/us_equities/monitoring/portfolio_monitor_summary.json"

    @property
    def holdings_template(self) -> Path:
        return self.root / "configs/us_equities/holdings_template.csv"

    # App state (git-ignored)
    @property
    def app_dir(self) -> Path:
        return self.root / "data/app"

    @property
    def jobs_dir(self) -> Path:
        return self.app_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.app_dir / "uploads"

    @property
    def settings_json(self) -> Path:
        return self.app_dir / "settings.json"

    def resolve(self, path: Optional[str]) -> Optional[Path]:
        """A config-relative path made absolute against the project root."""
        if not path:
            return None
        p = Path(path)
        return p if p.is_absolute() else self.root / p


def default_paths(root: Optional[Path] = None) -> Paths:
    return Paths(root=(root or Path.cwd()).resolve())
