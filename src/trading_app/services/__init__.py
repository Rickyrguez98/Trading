"""The app's service layer: the whole workflow behind one object.

``Workflow`` ties the paths, settings and job runner together and exposes one
method per user action. It is plain Python (no web framework), so it can back
any interface and is tested without a server.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from asset_selection.monitoring.broker import BrokerConfigError, init_holdings
from asset_selection.monitoring.config import BrokerConfig

from . import commands, status
from .commands import InvalidRequest
from .jobs import Job, JobConflict, JobManager
from .paths import STRATEGIES, Paths, default_paths
from .settings import AppSettings, load_settings, save_settings

__all__ = [
    "InvalidRequest", "Job", "JobConflict", "Paths", "STRATEGIES", "Workflow", "default_paths",
]

ALLOWED_UPLOADS = {".csv", ".pdf"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class Workflow:
    def __init__(self, paths: Optional[Paths] = None, jobs: Optional[JobManager] = None) -> None:
        self.paths = paths or default_paths()
        self.jobs = jobs or JobManager(self.paths.jobs_dir, cwd=self.paths.root)

    # -- settings -------------------------------------------------------
    @property
    def settings(self) -> AppSettings:
        return load_settings(self.paths)

    def set_broker_option(self, option: str) -> AppSettings:
        s = self.settings
        s.broker_option = str(option).strip().upper()
        save_settings(self.paths, s)
        return s

    # -- step 1 ---------------------------------------------------------
    def start_selection(self, mode: str = "full", limit: Optional[int] = None) -> Job:
        job_id = JobManager.new_id()
        job_dir = self.jobs.job_dir(job_id)
        argv = commands.selection_command(self.paths, mode=mode, limit=limit, job_dir=job_dir)
        outputs = ({"ranking": str(self.paths.ranking_csv), "summary": str(self.paths.selection_summary)}
                   if mode == "full" else
                   {"ranking": str(job_dir / "processed" / "asset_selection_results.csv"),
                    "summary": str(job_dir / "reports" / "asset_selection_summary.json")})
        title = "Full-universe stock selection" if mode == "full" else f"Sample selection ({limit or 50} tickers)"
        return self.jobs.start("selection", title, argv, outputs=outputs,
                               params={"mode": mode, "limit": limit}, job_id=job_id)

    # -- step 2 ---------------------------------------------------------
    def start_allocation(self, amount: float, strategy: Optional[str] = None, offline: bool = False,
                         instrument_prices: Optional[Dict[str, float]] = None) -> Job:
        argv = commands.allocation_command(self.paths, amount=amount, strategy=strategy, offline=offline,
                                           instrument_prices=instrument_prices)
        label = strategy or "all strategies"
        return self.jobs.start("allocation", f"Allocation of {float(amount):,.2f} ({label})", argv,
                               outputs={"summary": str(self.paths.allocation_summary)},
                               params={"amount": amount, "strategy": strategy, "offline": offline})

    # -- step 3 ---------------------------------------------------------
    def init_holdings(self) -> Path:
        cfg = BrokerConfig(holdings_csv=str(self.paths.resolve(self.settings.holdings_csv)))
        return init_holdings(cfg)

    def save_upload(self, filename: str, data: bytes) -> Path:
        suffix = Path(filename or "").suffix.lower()
        if suffix not in ALLOWED_UPLOADS:
            raise InvalidRequest("upload a .csv holdings file or a .pdf statement")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise InvalidRequest("the file is empty or larger than 10 MB")
        if suffix == ".pdf" and not data.startswith(b"%PDF"):
            raise InvalidRequest("that file is not a PDF")
        self.paths.uploads_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = self.paths.uploads_dir / f"upload-{stamp}{suffix}"
        dest.write_bytes(data)
        return dest

    def replace_holdings(self, data: bytes) -> Path:
        """Replace the option-B holdings CSV with an uploaded one, keeping a backup."""
        upload = self.save_upload("holdings.csv", data)
        from asset_selection.monitoring.holdings import load_holdings_csv

        try:
            load_holdings_csv(upload)        # refuse a file the monitor could not read
        except Exception as exc:  # noqa: BLE001
            upload.unlink(missing_ok=True)
            raise InvalidRequest(f"that CSV cannot be used as holdings: {exc}") from None
        dest = self.paths.resolve(self.settings.holdings_csv)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            shutil.copyfile(dest, dest.with_name(f"{dest.stem}.backup-{stamp}{dest.suffix}"))
        shutil.copyfile(upload, dest)
        return dest

    # -- step 4 ---------------------------------------------------------
    def start_monitor(self, *, option: Optional[str] = None, upload: Optional[Path] = None,
                      target_strategy: Optional[str] = None, cash: float = 0.0,
                      forecast: str = "bootstrap", refresh_signals: bool = False) -> Job:
        opt = str(option or self.settings.broker_option).strip().upper()
        if opt == "A" and upload is None and not self.settings.statement_pdf:
            raise InvalidRequest("option A needs a statement PDF: upload one")
        holdings = upload
        if holdings is None and opt == "B":
            holdings = self.paths.resolve(self.settings.holdings_csv)
            if holdings is None or not holdings.exists():
                raise InvalidRequest("no holdings file yet: create it on the Broker page")
        if holdings is None and opt == "A":
            holdings = self.paths.resolve(self.settings.statement_pdf)
        job_id = JobManager.new_id()
        out = self.jobs.job_dir(job_id) / "reports"
        argv = commands.monitor_command(self.paths, option=opt, output_dir=out, holdings=holdings,
                                        target_strategy=target_strategy or None, cash=cash,
                                        forecast=forecast, refresh_signals=refresh_signals)
        return self.jobs.start("monitor", f"Portfolio monitor (option {opt})", argv,
                               outputs={"summary": str(out / "portfolio_monitor_summary.json"),
                                        "report": str(out / "portfolio_monitor.md")},
                               params={"option": opt, "target": target_strategy, "cash": cash,
                                       "forecast": forecast}, job_id=job_id)

    # -- reads ----------------------------------------------------------
    def dashboard(self) -> Dict[str, object]:
        s = self.settings
        return {
            "ranking": status.ranking_info(self.paths),
            "plans": status.plan_infos(self.paths),
            "broker": status.broker_status(self.paths, s),
            "monitor": status.latest_monitor(self.paths, self.jobs),
            "jobs": self.jobs.list(limit=8),
        }


def safe_init_holdings(wf: Workflow) -> str:
    try:
        return f"Created {wf.init_holdings()}"
    except BrokerConfigError as exc:
        raise InvalidRequest(str(exc)) from None
