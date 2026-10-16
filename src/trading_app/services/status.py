"""Read the pipelines' output files into plain data for any front end.

Nothing here computes a recommendation: it reports what the pipelines last
produced, how old it is, and whether it can be trusted (run status), so the
interface never shows a number without its context.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from asset_selection.monitoring.broker import OPTIONS, readiness
from asset_selection.monitoring.config import BrokerConfig, load_config as load_monitoring_config

from .jobs import SUCCEEDED, JobManager
from .paths import STRATEGIES, Paths
from .settings import AppSettings

RANKING_COLUMNS = [
    "rank", "ticker", "company_name", "sector", "final_score", "fundamentals_score",
    "effective_sentiment_score", "risk_penalty", "candidate_role", "eligible_for_allocation", "last_close",
]
PLAN_COLUMNS = [
    "ticker", "name", "sleeve", "sector", "target_weight_pct", "realized_weight_pct", "price",
    "shares", "invested_value", "warnings",
]


def _records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age_days(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


# ---------------------------------------------------------------------------
# Step 1
# ---------------------------------------------------------------------------

@dataclass
class RankingInfo:
    exists: bool
    path: str
    generated_at: Optional[str] = None
    age_days: Optional[int] = None
    run_status: Optional[str] = None
    rows: int = 0
    stale: bool = False


def ranking_info(paths: Paths, max_age_days: int = 30) -> RankingInfo:
    info = RankingInfo(exists=paths.ranking_csv.exists(), path=str(paths.ranking_csv))
    if not info.exists:
        return info
    summary = _read_json(paths.selection_summary) or {}
    info.generated_at = summary.get("generated_at") or datetime.fromtimestamp(
        paths.ranking_csv.stat().st_mtime, tz=timezone.utc).isoformat()
    info.run_status = summary.get("run_status")
    info.age_days = _age_days(info.generated_at)
    try:
        info.rows = int(pd.read_csv(paths.ranking_csv, usecols=["ticker"]).shape[0])
    except (OSError, ValueError):
        info.rows = 0
    info.stale = info.age_days is not None and info.age_days > max_age_days
    return info


def top_ranking(paths: Paths, n: int = 25, csv: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = csv or paths.ranking_csv
    if not path.exists():
        return []
    df = pd.read_csv(path)
    if "rank" in df.columns:
        df = df.sort_values("rank")
    # Every expected column, even if an older ranking lacks some: missing -> None.
    return _records(df.reindex(columns=RANKING_COLUMNS).head(int(n)))


# ---------------------------------------------------------------------------
# Step 2
# ---------------------------------------------------------------------------

@dataclass
class PlanInfo:
    strategy: str
    path: str
    modified: str
    positions: int
    invested: float
    equity_pct: Optional[float] = None


def plan_infos(paths: Paths) -> List[PlanInfo]:
    summary = (_read_json(paths.allocation_summary) or {}).get("strategies", {})
    out = []
    for strategy, path in paths.plans().items():
        df = pd.read_csv(path)
        shares = pd.to_numeric(df.get("shares", pd.Series(dtype=float)), errors="coerce").fillna(0)
        invested = pd.to_numeric(df.get("invested_value", pd.Series(dtype=float)), errors="coerce").fillna(0)
        out.append(PlanInfo(
            strategy=strategy, path=str(path),
            modified=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            positions=int((shares > 0).sum()), invested=float(invested.sum()),
            equity_pct=(summary.get(strategy) or {}).get("equity_weight_pct"),
        ))
    order = {s: i for i, s in enumerate(STRATEGIES)}
    return sorted(out, key=lambda p: order.get(p.strategy, 99))


def plan_rows(paths: Paths, strategy: str) -> List[Dict[str, Any]]:
    path = paths.plan_csv(strategy)
    if strategy not in STRATEGIES or not path.exists():
        return []
    df = pd.read_csv(path)
    return _records(df.reindex(columns=PLAN_COLUMNS))


def allocation_summary(paths: Paths) -> Optional[Dict[str, Any]]:
    return _read_json(paths.allocation_summary)


# ---------------------------------------------------------------------------
# Step 3 / 4
# ---------------------------------------------------------------------------

def broker_status(paths: Paths, settings: AppSettings) -> Dict[str, Any]:
    try:
        base = load_monitoring_config(paths.monitoring_config).broker
    except Exception:  # noqa: BLE001 - defaults are fine for a status view
        base = BrokerConfig()
    cfg = BrokerConfig(
        option=settings.broker_option,
        holdings_csv=str(paths.resolve(settings.holdings_csv)),
        statement_pdf=str(paths.resolve(settings.statement_pdf)) if settings.statement_pdf else None,
        ibkr_token_env=base.ibkr_token_env, ibkr_query_id_env=base.ibkr_query_id_env,
    )
    root = f"{paths.root}/"
    rows = []
    for r in readiness(cfg):          # checked with absolute paths, shown relative to the project
        rows.append({"code": r.option.code, "label": r.option.label, "how": r.option.how,
                     "ready": r.ready, "detail": r.detail.replace(root, ""),
                     "selected": r.option.code == settings.broker_option})
    return {"selected": settings.broker_option, "options": rows,
            "holdings_csv": cfg.holdings_csv.replace(root, ""),
            "ibkr_env": [cfg.ibkr_token_env, cfg.ibkr_query_id_env]}


def holdings_rows(paths: Paths, settings: AppSettings) -> List[Dict[str, Any]]:
    path = paths.resolve(settings.holdings_csv)
    if path is None or not path.exists():
        return []
    return _records(pd.read_csv(path, dtype=str, keep_default_na=False))


@dataclass
class MonitorView:
    summary: Dict[str, Any]
    source: str
    generated_at: Optional[str]
    actions: Dict[str, int] = field(default_factory=dict)


def latest_monitor(paths: Paths, jobs: JobManager) -> Optional[MonitorView]:
    """The newest monitor result: the CLI's own output or the latest app job."""
    candidates = []
    canonical = _read_json(paths.monitor_summary)
    if canonical:
        candidates.append((canonical, str(paths.monitor_summary)))
    for job in jobs.list(limit=30):
        if job.kind == "monitor" and job.state == SUCCEEDED:
            data = _read_json(Path(job.outputs.get("summary", "")))
            if data:
                candidates.append((data, f"app job {job.id}"))
                break
    if not candidates:
        return None
    data, source = max(candidates, key=lambda c: c[0].get("generated_at") or "")
    actions: Dict[str, int] = {}
    for p in data.get("positions", []):
        a = (p.get("recommendation") or {}).get("action", "?")
        actions[a] = actions.get(a, 0) + 1
    return MonitorView(summary=data, source=source, generated_at=data.get("generated_at"), actions=actions)


# ---------------------------------------------------------------------------
# Dashboard charts
# ---------------------------------------------------------------------------

_NAME_NOISE = re.compile(r"\s+-\s+.*$|\s+(common stock|ordinary shares?|class [a-z] .*|depositary .*)$",
                         re.IGNORECASE)


def short_name(name: Optional[str]) -> Optional[str]:
    """``NVIDIA Corporation - Common Stock`` -> ``NVIDIA Corporation``."""
    if not name or not isinstance(name, str):
        return None
    return _NAME_NOISE.sub("", name.strip()) or name.strip()


def ranking_meta(paths: Paths) -> Dict[str, Dict[str, Any]]:
    """Sector and name per Yahoo symbol, from the latest ranking."""
    from .market import normalize_symbol

    if not paths.ranking_csv.exists():
        return {}
    try:
        df = pd.read_csv(paths.ranking_csv, usecols=lambda c: c in ("ticker", "company_name", "sector"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for rec in _records(df):
        try:
            sym = normalize_symbol(rec.get("ticker"))
        except ValueError:
            continue
        out[sym] = {"sector": rec.get("sector"), "name": short_name(rec.get("company_name"))}
    return out


def holdings_symbols(paths: Paths, settings: AppSettings) -> List[str]:
    """The Yahoo symbol of every position in the option-B holdings file."""
    from asset_selection.monitoring.holdings import load_holdings_csv

    from .market import normalize_symbol

    path = paths.resolve(settings.holdings_csv)
    if path is None or not path.exists():
        return []
    try:
        lots, _ = load_holdings_csv(path)
    except Exception:  # noqa: BLE001 - an unreadable file just contributes nothing here
        return []
    out = []
    for lot in lots:
        try:
            out.append(normalize_symbol(f"{lot.ticker}.MX" if lot.market == "BMV" else lot.ticker))
        except ValueError:
            continue
    return list(dict.fromkeys(out))


def default_watchlist(paths: Paths, settings: AppSettings, size: int = 8) -> List[str]:
    """What you own first, then the best-ranked stocks eligible for allocation."""
    from .market import normalize_symbol

    out = holdings_symbols(paths, settings)
    if paths.ranking_csv.exists() and len(out) < size:
        try:
            df = pd.read_csv(paths.ranking_csv)
        except (OSError, ValueError):
            df = pd.DataFrame()
        if "rank" in df.columns:
            df = df.sort_values("rank")
        if "eligible_for_allocation" in df.columns:
            df = df[df["eligible_for_allocation"].astype(str).str.lower() == "true"]
        for t in df.get("ticker", pd.Series(dtype=str)):
            try:
                sym = normalize_symbol(t)
            except ValueError:
                continue
            if sym not in out:
                out.append(sym)
            if len(out) >= size:
                break
    return out


def option_labels() -> Dict[str, str]:
    return {code: opt.label for code, opt in OPTIONS.items()}
