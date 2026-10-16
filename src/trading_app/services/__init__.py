"""The app's service layer: the whole workflow behind one object.

``Workflow`` ties the paths, settings and job runner together and exposes one
method per user action. It is plain Python (no web framework), so it can back
any interface and is tested without a server.
"""
from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from asset_selection.monitoring.broker import BrokerConfigError, init_holdings
from asset_selection.monitoring.config import BrokerConfig

from . import commands, status
from .commands import InvalidRequest
from .jobs import Job, JobConflict, JobManager
from .market import (
    BENCHMARK, DISPLAY_DAYS, MarketData, MarketDataError, chart_payload, clean, generated_at, normalize_symbol,
    sector_payload,
)
from .paths import STRATEGIES, Paths, default_paths
from .portfolio import Portfolio, PortfolioError
from .settings import AppSettings, load_settings, save_settings

__all__ = [
    "InvalidRequest", "Job", "JobConflict", "MarketDataError", "Paths", "Portfolio", "PortfolioError", "STRATEGIES",
    "WATCHLIST_MAX", "Workflow", "default_paths",
]

ALLOWED_UPLOADS = {".csv", ".pdf"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WATCHLIST_DEFAULT_SIZE = 8
WATCHLIST_MAX = 24


class Workflow:
    def __init__(self, paths: Optional[Paths] = None, jobs: Optional[JobManager] = None,
                 market: Optional[MarketData] = None) -> None:
        self.paths = paths or default_paths()
        self.jobs = jobs or JobManager(self.paths.jobs_dir, cwd=self.paths.root)
        self.market = market or MarketData(self.paths.app_dir / "cache" / "market")
        self.portfolio = Portfolio(self.paths, self.market)

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
        tickers = self.my_tickers() if mode == "mine" else None
        argv = commands.selection_command(self.paths, mode=mode, limit=limit, job_dir=job_dir, tickers=tickers)
        outputs = ({"ranking": str(self.paths.ranking_csv), "summary": str(self.paths.selection_summary)}
                   if mode == "full" else
                   {"ranking": str(job_dir / "processed" / "asset_selection_results.csv"),
                    "summary": str(job_dir / "reports" / "asset_selection_summary.json")})
        title = {"full": "Full-universe stock selection",
                 "mine": f"My stocks only ({len(tickers or [])} tickers)"}.get(
            mode, f"Sample selection ({limit or 50} tickers)")
        return self.jobs.start("selection", title, argv, outputs=outputs,
                               params={"mode": mode, "limit": limit, "tickers": tickers}, job_id=job_id)

    def my_tickers(self) -> List[str]:
        """US tickers you hold: open positions from your trade history, else the holdings file."""
        if self.portfolio.ledger_path.exists():
            symbols = self.portfolio.view().open_symbols
        else:
            symbols = [s for s in status.holdings_symbols(self.paths, self.settings) if not s.endswith(".MX")]
        return [s.replace("-", ".") for s in symbols]

    def my_stocks_analysis(self) -> Optional[Dict[str, Any]]:
        """The latest finished my-stocks run: fundamentals, news and scores for what you hold."""
        for job in self.jobs.list(limit=50):
            if job.kind == "selection" and (job.params or {}).get("mode") == "mine" and job.state == "succeeded":
                rows = status.top_ranking(self.paths, 500, csv=Path(job.outputs.get("ranking", "")),
                                          columns=status.ANALYSIS_COLUMNS)
                if rows:
                    return {"job": job, "rows": rows}
        return None

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

    # -- dashboard charts -------------------------------------------------
    def watchlist(self) -> List[str]:
        saved = self.settings.watchlist
        if saved is not None:
            return list(saved)
        return status.default_watchlist(self.paths, self.settings, WATCHLIST_DEFAULT_SIZE,
                                        owned=self.owned_symbols(), limit=WATCHLIST_MAX)

    def owned_symbols(self) -> List[str]:
        """Open positions from your imported trade confirmations, as Yahoo symbols."""
        if not self.portfolio.ledger_path.exists():
            return []
        return [normalize_symbol(s) for s in self.portfolio.view().open_symbols]

    def add_to_watchlist(self, raw: str) -> List[str]:
        symbol = _symbol(raw)
        current = self.watchlist()
        if symbol in current:
            return current
        if len(current) >= WATCHLIST_MAX:
            raise InvalidRequest(f"the watchlist holds up to {WATCHLIST_MAX} symbols; remove one first")
        try:
            self.market.bars(symbol)           # refuse a symbol Yahoo has no prices for
        except MarketDataError as exc:
            raise InvalidRequest(str(exc)) from None
        return self._save_watchlist(current + [symbol])

    def remove_from_watchlist(self, raw: str) -> List[str]:
        symbol = _symbol(raw)
        return self._save_watchlist([x for x in self.watchlist() if x != symbol])

    def reset_watchlist(self) -> List[str]:
        s = self.settings
        s.watchlist = None
        save_settings(self.paths, s)
        return self.watchlist()

    def _save_watchlist(self, symbols: List[str]) -> List[str]:
        s = self.settings
        s.watchlist = symbols
        save_settings(self.paths, s)
        return list(s.watchlist)

    def market_chart(self, raw: str, since: Optional[date] = None) -> Dict[str, Any]:
        """One stock: a year of candles (or back to ``since``), indicators, Fibonacci levels, statistics."""
        symbol = _symbol(raw)
        days = DISPLAY_DAYS
        start = None
        if since is not None:
            days = max(DISPLAY_DAYS, (date.today() - since).days + 30)
            start = since - timedelta(days=330)          # the SMA200 needs ~200 trading days before
        payload = chart_payload(symbol, self.market.bars(symbol, start), days=days)
        meta = self.market.meta(symbol, status.ranking_meta(self.paths))
        payload.update(sector=meta.get("sector"), name=status.short_name(meta.get("name")))
        return clean(payload)

    def market_sectors(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """The watchlist (or the given symbols) grouped by sector, each against the S&P 500."""
        known = status.ranking_meta(self.paths)
        groups: Dict[str, Dict[str, Any]] = {}
        names: Dict[str, Optional[str]] = {}
        errors: Dict[str, str] = {}
        for symbol in ([_symbol(s) for s in symbols][:WATCHLIST_MAX * 2] if symbols else self.watchlist()):
            try:
                frame = self.market.bars(symbol)
            except MarketDataError as exc:
                errors[symbol] = str(exc)
                continue
            meta = self.market.meta(symbol, known)
            names[symbol] = status.short_name(meta.get("name"))
            groups.setdefault(meta.get("sector") or "Other", {})[symbol] = frame
        try:
            benchmark = self.market.bars(BENCHMARK)
        except MarketDataError as exc:
            benchmark, errors[BENCHMARK] = None, str(exc)
        sectors = []
        for name in sorted(groups, key=lambda n: (-len(groups[n]), n)):
            try:
                sectors.append(sector_payload(name, groups[name], benchmark=benchmark))
            except MarketDataError as exc:
                errors[name] = str(exc)
        return clean({"sectors": sectors, "names": names, "benchmark": "S&P 500" if benchmark is not None else None,
                      "errors": errors, "generated_at": generated_at()})

    # -- reads ----------------------------------------------------------
    def dashboard(self) -> Dict[str, object]:
        s = self.settings
        return {
            "ranking": status.ranking_info(self.paths),
            "plans": status.plan_infos(self.paths),
            "broker": status.broker_status(self.paths, s),
            "holdings": len(self.owned_symbols()) or len(status.holdings_symbols(self.paths, s)),
            "holdings_source": "trade confirmations" if self.owned_symbols() else "holdings file",
            "monitor": status.latest_monitor(self.paths, self.jobs),
            "jobs": self.jobs.list(limit=8),
        }


def _symbol(raw: str) -> str:
    try:
        return normalize_symbol(raw)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None


def safe_init_holdings(wf: Workflow) -> str:
    try:
        return f"Created {wf.init_holdings()}"
    except BrokerConfigError as exc:
        raise InvalidRequest(str(exc)) from None
