"""Professional output layer for the binary-options pipeline.

Layout (per spec):

    data/binary_options/processed/signals_all.csv
    data/binary_options/processed/signals_short_term.csv
    data/binary_options/processed/signals_extended_intraday.csv

    reports/binary_options/runs/<run_id>/   -- immutable per-run artifacts
    reports/binary_options/latest/          -- copy of the latest completed run

    each run directory contains:
        signals_all.md, signals_short_term.md, signals_extended_intraday.md
        run_summary.json
        provider_diagnostics.{json,md}
        output_validation.{json,md}         (written by the pipeline afterwards)
        run_manifest.json

Historical runs are never overwritten; ``latest/`` is refreshed atomically at
the end of a completed run.
"""
from __future__ import annotations

import hashlib
import json
import platform as _platform
import shutil
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from trading_common.io import ensure_dir, write_csv, write_json

from .config import HORIZON_GROUP_EXTENDED, HORIZON_GROUP_SHORT, BinaryOptionsConfig
from .signals.decision import SIGNAL_NO_TRADE
from .validation.run_status import RunAssessment, render_status_banner

_LIST_FIELDS = ("primary_drivers", "primary_conflicts", "warning_flags")
_DICT_FIELDS = ("family_contributions", "missing_families", "feature_values")


# ---------------------------------------------------------------------------
# CSV / flattening
# ---------------------------------------------------------------------------

def _flatten_for_csv(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        c = dict(r)
        for k in _LIST_FIELDS:
            c[k] = "; ".join(c.get(k) or [])
        for k in _DICT_FIELDS:
            c[k] = "; ".join(f"{a}={b}" for a, b in (c.get(k) or {}).items())
        out.append(c)
    return out


def write_signal_csvs(cfg: BinaryOptionsConfig, rows: List[Dict[str, Any]]) -> Dict[str, str]:
    processed = ensure_dir(cfg.run.processed_dir)
    paths: Dict[str, str] = {}
    groups = {
        "signals_all": rows,
        "signals_short_term": [r for r in rows if r["horizon_group"] == HORIZON_GROUP_SHORT],
        "signals_extended_intraday": [
            r for r in rows if r["horizon_group"] == HORIZON_GROUP_EXTENDED
        ],
    }
    for name, subset in groups.items():
        p = processed / f"{name}.csv"
        write_csv(p, pd.DataFrame(_flatten_for_csv(subset)))
        paths[f"{name}_csv"] = str(p)
    return paths


# ---------------------------------------------------------------------------
# Horizon matrix
# ---------------------------------------------------------------------------

def horizon_matrix(rows: List[Dict[str, Any]], horizons: List[int]) -> Dict[str, Dict[str, str]]:
    """instrument -> {"5m": "CALL", ...} for every requested horizon."""
    matrix: Dict[str, Dict[str, str]] = {}
    for r in rows:
        sym = r["underlying_symbol"]
        matrix.setdefault(sym, {})[f"{r['horizon_minutes']}m"] = r["signal"]
    # every requested horizon appears, even if a row is somehow absent
    for sym, cells in matrix.items():
        for h in horizons:
            cells.setdefault(f"{h}m", SIGNAL_NO_TRADE)
    return dict(sorted(matrix.items()))


def _matrix_md(matrix: Dict[str, Dict[str, str]], horizons: List[int]) -> List[str]:
    if not matrix:
        return ["_No instruments._"]
    heads = [f"{h}m" for h in horizons]
    lines = ["| Instrument | " + " | ".join(heads) + " |",
             "|---" * (len(heads) + 1) + "|"]
    for sym, cells in matrix.items():
        lines.append(
            f"| {sym} | " + " | ".join(cells.get(h, SIGNAL_NO_TRADE) for h in heads) + " |"
        )
    return lines


# ---------------------------------------------------------------------------
# Markdown reports
# ---------------------------------------------------------------------------

def _fmt_age(v: Optional[float]) -> str:
    return f"{v:.0f}s" if v is not None else "—"


def _signal_table(rows: List[Dict[str, Any]], top_n: int) -> List[str]:
    if not rows:
        return ["_None._"]
    lines = [
        "| # | instrument | platform | horizon | action | strength | dir | conf "
        "| quality | payout | break-even | data age | drivers | warnings |",
        "|---" * 14 + "|",
    ]
    for i, r in enumerate(rows[:top_n], 1):
        be = r["break_even_win_probability"]
        be_txt = f"{be:.1%}" if be is not None else "—"
        payout_txt = r["payout_ratio"] if r["payout_ratio"] is not None else "—"
        lines.append(
            f"| {i} | {r['underlying_symbol']} | {r['platform'] or '—'} "
            f"| {r['horizon_minutes']}m ({r['horizon_group']}) "
            f"| **{r['recommended_action']}** | {r['signal_strength']} "
            f"| {r['directional_score']:+.1f} | {r['confidence_score']:.2f} "
            f"| {r['data_quality_score']:.0f} "
            f"| {payout_txt} | {be_txt} "
            f"| {_fmt_age(r['market_data_age_seconds'])} "
            f"| {', '.join(r['primary_drivers']) or '—'} "
            f"| {', '.join(r['warning_flags']) or '—'} |"
        )
    return lines


def render_signals_md(
    cfg: BinaryOptionsConfig,
    rows: List[Dict[str, Any]],
    assessment: RunAssessment,
    generated_at: str,
    *,
    title: str,
    horizons: List[int],
    catalog_notes: List[str],
    include_matrix: bool = True,
) -> str:
    actionable = [r for r in rows if r["signal"] != SIGNAL_NO_TRADE]
    calls = [r for r in actionable if r["signal"] == "CALL"]
    puts = [r for r in actionable if r["signal"] == "PUT"]
    no_trade = [r for r in rows if r["signal"] == SIGNAL_NO_TRADE]

    lines: List[str] = [f"# {title}", ""]
    lines.append(render_status_banner(assessment, generated_at))
    lines.append("")
    lines.append("## How to read this report")
    lines.append("")
    lines.append(
        "- Each row is one **contract instrument × expiration horizon**. "
        "`BUY_CALL` = evidence favors settlement **above** the contract "
        "reference price; `BUY_PUT` = **below** it. Neither means buying or "
        "selling the underlying asset itself."
    )
    lines.append(
        "- `NO_TRADE` is a first-class answer with a machine-readable reason: "
        "unavailable contracts, unavailable expirations, stale catalogs or "
        "data, delayed feeds, bad economics, weak or contradictory evidence."
    )
    lines.append(
        "- `conf` is a bounded **heuristic** (margin × agreement × data quality "
        "× coverage × volatility regime). It is **not a probability** and must "
        "not be compared with the break-even column as if it were a win rate."
    )
    lines.append(
        "- payout ratio = net profit per unit staked; break-even win "
        "probability = 1 / (1 + payout)."
    )
    lines.append("")

    if include_matrix:
        lines.append("## Complete horizon matrix")
        lines.append("")
        lines += _matrix_md(horizon_matrix(rows, horizons), horizons)
        lines.append("")

    lines.append(f"## Best valid CALL opportunities ({len(calls)})")
    lines.append("")
    lines += _signal_table(calls, cfg.run.top_n)
    lines.append("")
    lines.append(f"## Best valid PUT opportunities ({len(puts)})")
    lines.append("")
    lines += _signal_table(puts, cfg.run.top_n)
    lines.append("")

    lines.append(f"## NO_TRADE results ({len(no_trade)})")
    lines.append("")
    reason_counts: Dict[str, int] = {}
    for r in no_trade:
        reason_counts[r["no_trade_reason"]] = reason_counts.get(r["no_trade_reason"], 0) + 1
    if reason_counts:
        lines.append("| reason | rows |")
        lines.append("|---|---|")
        for reason, n in sorted(reason_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            lines.append(f"| {reason} | {n} |")
    else:
        lines.append("_None._")
    lines.append("")

    unavailable = [r for r in no_trade if r["no_trade_reason"] == "EXPIRATION_NOT_AVAILABLE"]
    lines.append(f"## Unavailable expirations ({len(unavailable)})")
    lines.append("")
    if unavailable:
        lines.append("| instrument | horizon |")
        lines.append("|---|---|")
        for r in unavailable:
            lines.append(f"| {r['underlying_symbol']} | {r['horizon_minutes']}m |")
    else:
        lines.append("_None._")
    lines.append("")

    stale_reasons = {
        "CATALOG_STALE", "STALE_MARKET_DATA", "DATA_NOT_REAL_TIME",
        "PLATFORM_QUOTE_STALE", "INSTRUMENT_AVAILABILITY_UNKNOWN",
        "INSTRUMENT_NOT_AVAILABLE", "CONTRACT_CLOSED", "PROVIDER_UNAVAILABLE",
    }
    stale = [r for r in no_trade if r["no_trade_reason"] in stale_reasons]
    lines.append(f"## Stale or invalid instruments ({len(stale)})")
    lines.append("")
    if stale:
        lines.append("| instrument | horizon | reason | catalog age | data age |")
        lines.append("|---|---|---|---|---|")
        for r in stale:
            lines.append(
                f"| {r['underlying_symbol']} | {r['horizon_minutes']}m "
                f"| {r['no_trade_reason']} | {_fmt_age(r['catalog_age_seconds'])} "
                f"| {_fmt_age(r['market_data_age_seconds'])} |"
            )
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Provider and catalog limitations")
    lines.append("")
    for n in catalog_notes or ["No catalog notes recorded."]:
        lines.append(f"- {n}")
    lines.append(
        "- Market data: yfinance intraday data is delayed (~15 min) and "
        "unofficial; it is never treated as real-time, which is why default "
        "short-term rows are gated (DATA_NOT_REAL_TIME)."
    )
    lines.append("")
    lines.append("---")
    lines.append(
        "> **Methodology disclaimer.** Research and educational output only — "
        "not financial advice. Binary options are high-risk, all-or-nothing "
        "instruments; many platforms operate under weak regulation. Signals "
        "are heuristic research outputs with **no measured accuracy claim**; "
        "historical behavior does not guarantee future results; nothing here "
        "executes trades or recommends stake sizes."
    )
    lines.append("")
    return "\n".join(lines)


def render_diagnostics_md(diag: Dict[str, Any]) -> str:
    lines = [
        "# Binary options provider diagnostics",
        "",
        f"Run `{diag['run_id']}` — market data **{diag['market_data_provider']}**"
        f" — catalog **{diag.get('catalog_provider') or 'none'}** — {diag['generated_at']}",
        "",
    ]
    hc = diag.get("health_check")
    if hc:
        lines.append(
            f"Health probe: {hc['ok_count']} ok / {hc['fail_count']} failed"
            + (" — **SYSTEMIC FAILURE**" if hc["systemic_failure"] else "")
        )
        lines.append("")
    cat = diag.get("catalog")
    if cat:
        lines.append(
            f"Catalog: platform={cat.get('platform')} instruments={cat.get('instruments')} "
            f"age={_fmt_age(cat.get('age_seconds'))} stale={cat.get('stale')}"
        )
        lines.append("")
    lines.append(
        "| symbol | status | error_type | source | candles | latest close | "
        "quality | stale | usable |"
    )
    lines.append("|---" * 9 + "|")
    for s in diag["symbols"]:
        lines.append(
            f"| {s['symbol']} | {s['status']} | {s['error_type'] or '—'} "
            f"| {s['data_source'] or '—'} | {s['candle_count']} "
            f"| {s['latest_candle_close'] or '—'} "
            f"| {s['data_quality_score'] if s['data_quality_score'] is not None else '—'} "
            f"| {s['stale']} | {s['usable']} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

def _git_state() -> Dict[str, Any]:
    def _run(args):
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=5
            ).stdout.strip() or None
        except Exception:  # noqa: BLE001 - manifest metadata is best-effort
            return None

    commit = _run(["rev-parse", "HEAD"])
    dirty = _run(["status", "--porcelain"])
    return {"git_commit": commit, "git_dirty_state": bool(dirty) if dirty is not None else None}


def _sha256_file(path: Optional[str]) -> Optional[str]:
    if not path or not Path(path).exists():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _package_version() -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("asset-selection")
    except Exception:  # noqa: BLE001
        return None


def _config_snapshot(cfg: BinaryOptionsConfig) -> Dict[str, Any]:
    def clean(obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            return {k: clean(v) for k, v in asdict(obj).items()}
        if isinstance(obj, dict):
            return {str(k): clean(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [clean(v) for v in obj]
        return obj

    return clean(cfg)


def build_run_manifest(
    *,
    cfg: BinaryOptionsConfig,
    run_id: str,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    config_path: Optional[str],
    catalog_path: Optional[str],
    catalog_hash: Optional[str],
    market_data_provider: str,
    catalog_provider: Optional[str],
    output_paths: Dict[str, str],
    run_status: str,
) -> Dict[str, Any]:
    snapshot = _config_snapshot(cfg)
    providers: Dict[str, Any] = {"market_data": market_data_provider,
                                 "catalog": catalog_provider}
    provider_versions: Dict[str, Optional[str]] = {}
    try:
        import yfinance

        provider_versions["yfinance"] = getattr(yfinance, "__version__", None)
    except Exception:  # noqa: BLE001 - optional at manifest time
        provider_versions["yfinance"] = None
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        **_git_state(),
        "python_version": sys.version.split()[0],
        "platform": _platform.platform(),
        "package_version": _package_version(),
        "configuration_path": config_path,
        "configuration_hash": _sha256_file(config_path)
        or hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest(),
        "configuration_snapshot": snapshot,
        "providers": providers,
        "provider_versions": provider_versions,
        "catalog_path": catalog_path,
        "catalog_hash": catalog_hash,
        "requested_horizons": cfg.horizons.minutes,
        "requested_interval": cfg.candles.interval,
        "timezone": cfg.market_session.timezone,
        "random_seed_if_any": None,
        "output_paths": output_paths,
        "run_status": run_status,
    }
    return manifest


# ---------------------------------------------------------------------------
# Run directory layout
# ---------------------------------------------------------------------------

def run_dir_for(cfg: BinaryOptionsConfig, run_id: str) -> Path:
    return Path(cfg.run.output_dir) / "runs" / run_id


def latest_dir_for(cfg: BinaryOptionsConfig) -> Path:
    return Path(cfg.run.output_dir) / "latest"


def refresh_latest(cfg: BinaryOptionsConfig, run_id: str) -> str:
    """Copy the completed run's artifacts into ``latest/`` (replace-in-place).

    Historical runs stay untouched under ``runs/<run_id>/``.
    """
    src = run_dir_for(cfg, run_id)
    dst = latest_dir_for(cfg)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return str(dst)


def write_json_and_md(path_base: Path, name: str, payload: Dict[str, Any], md: str) -> Dict[str, str]:
    write_json(path_base / f"{name}.json", payload)
    (path_base / f"{name}.md").write_text(md, encoding="utf-8")
    return {f"{name}_json": str(path_base / f"{name}.json"),
            f"{name}_md": str(path_base / f"{name}.md")}
