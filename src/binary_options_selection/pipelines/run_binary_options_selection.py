"""Staged binary-options selection pipeline.

Stages (each records input/output counts, duration, drops, and failures):

    1. Universe            custom | watchlist | asset-selection shortlist
    2. Session + health    market-state detection, optional provider probe
    3. Intraday candles    fetch per symbol (+ benchmark), provenance recorded
    4. Quality screening   candle integrity, staleness, liquidity
    5-7. Signals           per (symbol x horizon): features -> scores -> decision
    8. Ranking             deterministic; NO_TRADE rows separated, never mixed
    9. Outputs             CSV + Markdown + JSON + provider diagnostics
    10. Output validation  post-run self-audit

Outputs:
    data/processed/binary_options_candidates.csv
    reports/binary_options_candidates.md
    reports/binary_options_summary.json
    reports/binary_options_provider_diagnostics.{md,json}
    reports/binary_options_output_validation.{md,json}

Research signals only: no execution, no broker, no stake sizing, and the
confidence score is a bounded heuristic — never a probability.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from trading_common.cache import Cache
from trading_common.io import ensure_dir, write_csv, write_json
from trading_common.rate_limiter import RateLimiter

from ..config import (
    SUPPORTED_INTERVALS,
    BinaryOptionsConfig,
    ConfigValidationError,
    horizon_group,
    load_config,
    validate_config,
)
from ..data_providers import build_intraday_provider
from ..data_providers import errors as err
from ..data_providers.base import IntradayCandlesProvider, IntradaySeries
from ..features.engine import compute_features
from ..health.provider_health import run_intraday_health_check
from ..market_session import classify_session, expiry_for
from ..scoring.directional import DirectionalScores, score_direction
from ..signals.decision import (
    SIGNAL_NO_TRADE,
    assess_liquidity,
    decide_signal,
)
from ..signals.payout import break_even_win_probability
from ..universe import resolve_universe
from ..utils.timeutils import iso_utc, utc_now
from ..validation.candles import SeriesQuality, assess_series
from ..validation.output_validation import render_markdown as render_validation_md
from ..validation.output_validation import validate_outputs
from ..validation.run_status import (
    RUN_INVALID,
    determine_run_status,
    render_status_banner,
)

logger = logging.getLogger(__name__)

_LOG_CONFIGURED = False


def _configure_logging(level: Optional[str]) -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    resolved = (level or os.environ.get("BINARY_OPTIONS_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    for noisy in ("urllib3", "yfinance", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _LOG_CONFIGURED = True


# ---------------------------------------------------------------------------
# Stage stats
# ---------------------------------------------------------------------------

@dataclass
class StageStats:
    name: str
    input_count: int = 0
    output_count: int = 0
    duration_seconds: float = 0.0
    provider_failures: int = 0
    dropped: Dict[str, int] = field(default_factory=dict)
    warnings: int = 0
    notes: List[str] = field(default_factory=list)

    def drop(self, reason: str, n: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + n

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "duration_seconds": round(self.duration_seconds, 2),
            "provider_failures": self.provider_failures,
            "dropped": self.dropped,
            "warnings": self.warnings,
            "notes": self.notes,
        }


class _Timer:
    def __init__(self, stage: StageStats) -> None:
        self.stage = stage

    def __enter__(self):
        self._t0 = time.time()
        return self.stage

    def __exit__(self, *exc) -> None:
        self.stage.duration_seconds = time.time() - self._t0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    cfg: BinaryOptionsConfig,
    *,
    cli_tickers: Optional[List[str]] = None,
    cli_watchlist_file: Optional[str] = None,
    provider: Optional[IntradayCandlesProvider] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run the staged pipeline. ``provider`` and ``now`` are injectable so
    tests run deterministically offline."""
    now = now or utc_now()
    run_id = uuid.uuid4().hex[:12]
    generated_at = iso_utc(now)
    stages: List[StageStats] = []
    interval = cfg.candles.interval
    interval_minutes = SUPPORTED_INTERVALS[interval]
    interval_seconds = interval_minutes * 60

    # --- Stage 1: universe -------------------------------------------------
    s1 = StageStats("1_universe")
    with _Timer(s1):
        universe = resolve_universe(
            cfg, cli_tickers=cli_tickers, cli_watchlist_file=cli_watchlist_file
        )
        s1.input_count = len(universe.symbols) + len(universe.dropped)
        s1.output_count = len(universe.symbols)
        for sym, reason in universe.dropped.items():
            s1.drop(reason)
        s1.notes.extend(universe.notes)
        if universe.error:
            s1.notes.append(universe.error)
    stages.append(s1)
    logger.info("Stage 1 universe: %d symbols (%s mode)%s",
                len(universe.symbols), universe.mode,
                f" ERROR: {universe.error}" if universe.error else "")

    # --- Stage 2: session + provider health --------------------------------
    if provider is None:
        cache = Cache(
            directory=cfg.cache.dir, enabled=cfg.cache.enabled,
            default_ttl=cfg.cache.ttl_for_interval(interval_seconds),
        )
        limiter = RateLimiter(cfg.rate_limits.get(cfg.providers.intraday, 0.0))
        provider = build_intraday_provider(cfg.providers.intraday, cache, limiter)
    ttl = cfg.cache.ttl_for_interval(interval_seconds)

    s2 = StageStats("2_session_health")
    systemic_failure = False
    with _Timer(s2):
        session = classify_session(now, cfg.market_session)
        s2.notes.append(f"market_state={session.state} ({session.exchange_local_time})")
        health_summary = None
        if cfg.robustness.run_health_check and not universe.error:
            health_summary = run_intraday_health_check(provider, cfg)
            systemic_failure = health_summary.systemic_failure
            s2.provider_failures = health_summary.fail_count
            if systemic_failure:
                s2.notes.append("systemic provider failure on benchmark symbols")
        s2.input_count = s2.output_count = len(universe.symbols)
    stages.append(s2)
    logger.info("Stage 2 session=%s systemic_failure=%s", session.state, systemic_failure)

    # --- Stage 3: fetch candles --------------------------------------------
    s3 = StageStats("3_fetch_candles")
    series_by_symbol: Dict[str, IntradaySeries] = {}
    benchmark_series: Optional[IntradaySeries] = None
    with _Timer(s3):
        s3.input_count = len(universe.symbols)
        if not universe.error and not systemic_failure:
            include_ext = cfg.market_session.session == "extended"
            for sym in universe.symbols:
                series = provider.fetch(
                    sym, interval=interval, bars=cfg.candles.fetch_history_bars,
                    include_extended_hours=include_ext, ttl_seconds=ttl,
                )
                series_by_symbol[sym] = series
                if series.status != "ok":
                    s3.provider_failures += 1
                    s3.drop(series.error_type or "unknown_failure")
            bench = cfg.universe.benchmark_symbol
            if bench in series_by_symbol:
                benchmark_series = series_by_symbol[bench]
            elif bench:
                benchmark_series = provider.fetch(
                    bench, interval=interval, bars=cfg.candles.fetch_history_bars,
                    include_extended_hours=include_ext, ttl_seconds=ttl,
                )
                if benchmark_series.status != "ok":
                    s3.notes.append(
                        f"benchmark {bench} unavailable: {benchmark_series.error_type}"
                    )
        s3.output_count = sum(1 for s in series_by_symbol.values() if s.status == "ok")
    stages.append(s3)

    # --- Stage 4: quality + liquidity screening ----------------------------
    s4 = StageStats("4_quality_screen")
    quality_by_symbol: Dict[str, SeriesQuality] = {}
    liquidity_by_symbol: Dict[str, Any] = {}
    benchmark_candles = None
    with _Timer(s4):
        s4.input_count = len(series_by_symbol)
        for sym, series in series_by_symbol.items():
            q = assess_series(series, cfg, now)
            quality_by_symbol[sym] = q
            liquidity_by_symbol[sym] = assess_liquidity(q.candles, cfg)
            if q.flags:
                s4.warnings += len(q.flags)
            if not q.usable:
                s4.drop(q.error_type or "quality_below_minimum")
        if benchmark_series is not None and benchmark_series.status == "ok":
            bq = assess_series(benchmark_series, cfg, now)
            if bq.candles and not bq.stale:
                benchmark_candles = bq.candles
            else:
                s4.notes.append("benchmark series stale or empty; context family disabled")
        s4.output_count = sum(1 for q in quality_by_symbol.values() if q.usable)
    stages.append(s4)

    # --- Stages 5-7: features -> scores -> decisions ------------------------
    s5 = StageStats("5_signals")
    rows: List[Dict[str, Any]] = []
    payout_ratio = cfg.payout.payout_ratio
    break_even = (
        round(break_even_win_probability(payout_ratio), 4)
        if payout_ratio is not None else None
    )
    with _Timer(s5):
        s5.input_count = len(universe.symbols) * len(cfg.horizons.minutes)
        for sym in universe.symbols:
            series = series_by_symbol.get(sym)
            quality = quality_by_symbol.get(sym) or SeriesQuality(
                symbol=sym, error_type=err.NO_INTRADAY_DATA
            )
            liquidity = liquidity_by_symbol.get(sym) or assess_liquidity([], cfg)
            sentiment_score = (
                universe.sentiment_by_symbol.get(sym)
                if cfg.sentiment.enabled else None
            )
            for horizon in cfg.horizons.minutes:
                if quality.candles:
                    bundle = compute_features(
                        quality.candles,
                        horizon_minutes=horizon,
                        interval_minutes=interval_minutes,
                        cfg=cfg,
                        benchmark_candles=(
                            benchmark_candles if sym != cfg.universe.benchmark_symbol
                            else None
                        ),
                        sentiment_score=sentiment_score,
                    )
                    scores = score_direction(
                        bundle, data_quality_score=quality.data_quality_score, cfg=cfg
                    )
                    vol_flags = bundle.flags
                    feature_values = {
                        k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in bundle.values.items() if v is not None
                    }
                else:
                    scores = DirectionalScores(
                        horizon_minutes=horizon, horizon_group=horizon_group(horizon)
                    )
                    vol_flags = []
                    feature_values = {}
                reference_time = quality.latest_complete_close
                expiry = (
                    expiry_for(reference_time, horizon)
                    if reference_time is not None
                    else now + timedelta(minutes=horizon)
                )
                decision = decide_signal(
                    scores, quality, session, liquidity, expiry_utc=expiry, cfg=cfg
                )
                decision.warning_flags.extend(vol_flags)
                rows.append(_build_row(
                    run_id=run_id, generated_at=generated_at, symbol=sym,
                    company=universe.name_by_symbol.get(sym),
                    series=series, quality=quality, scores=scores,
                    decision=decision, session_state=session.state,
                    interval=interval, reference_time=reference_time,
                    expiry=expiry if reference_time is not None else None,
                    horizon=horizon, payout_ratio=payout_ratio,
                    break_even=break_even, timezone_name=cfg.market_session.timezone,
                    feature_values=feature_values,
                ))
        s5.output_count = len(rows)
        for r in rows:
            if r["signal"] == SIGNAL_NO_TRADE:
                s5.drop(f"NO_TRADE:{r['no_trade_reason']}")
    stages.append(s5)

    # --- Stage 8: ranking ---------------------------------------------------
    s8 = StageStats("8_ranking")
    with _Timer(s8):
        s8.input_count = len(rows)
        rows = _rank_rows(rows)
        s8.output_count = sum(1 for r in rows if r["signal"] != SIGNAL_NO_TRADE)
    stages.append(s8)

    # --- Run status ----------------------------------------------------------
    assessment = determine_run_status(
        universe_error=universe.error,
        symbols_requested=len(universe.symbols),
        symbols_fetched=sum(1 for s in series_by_symbol.values() if s.status == "ok"),
        provider_side_failures=sum(
            1 for s in series_by_symbol.values() if err.is_provider_side(s.error_type)
        ),
        systemic_health_failure=systemic_failure,
        rows=rows,
        benchmark_available=benchmark_candles is not None,
        market_state=session.state,
        allow_partial_results=cfg.robustness.allow_partial_results,
        max_provider_failure_ratio=cfg.robustness.max_provider_failure_ratio,
    )
    for r in rows:
        r["run_status"] = assessment.run_status

    # --- Stage 9: outputs ----------------------------------------------------
    s9 = StageStats("9_outputs")
    with _Timer(s9):
        paths = _write_outputs(
            cfg, rows, stages, assessment, universe, series_by_symbol,
            quality_by_symbol, health_summary, session, run_id, generated_at,
            payout_ratio, break_even,
        )
        s9.output_count = len(paths)
    stages.append(s9)

    # --- Stage 10: output validation ------------------------------------------
    validation_result = None
    if cfg.validation.enabled:
        validation_result = validate_outputs(rows, run_status=assessment.run_status)
        out_dir = ensure_dir(cfg.run.output_dir)
        write_json(out_dir / "binary_options_output_validation.json", validation_result)
        (out_dir / "binary_options_output_validation.md").write_text(
            render_validation_md(validation_result), encoding="utf-8"
        )
        logger.info("Output validation: %s", validation_result["overall_status"])

    return {
        "run_id": run_id,
        "rows": rows,
        "assessment": assessment,
        "stages": stages,
        "paths": paths,
        "validation": validation_result,
        "session": session,
    }


# ---------------------------------------------------------------------------
# Row construction / ranking
# ---------------------------------------------------------------------------

def _build_row(
    *, run_id, generated_at, symbol, company, series, quality, scores,
    decision, session_state, interval, reference_time, expiry, horizon,
    payout_ratio, break_even, timezone_name, feature_values,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "symbol": symbol,
        "company_name": company,
        "provider_symbol": getattr(series, "provider_symbol", None) if series else None,
        "provider": getattr(series, "provider", None) if series else None,
        "provider_status": getattr(series, "status", "error") if series else "error",
        "data_source": getattr(series, "data_source", None) if series else None,
        "market_state": session_state,
        "timezone": timezone_name,
        "interval": interval,
        "latest_candle_close": (
            iso_utc(quality.latest_complete_close)
            if quality.latest_complete_close else None
        ),
        "data_age_seconds": (
            round(quality.data_age_seconds, 1)
            if quality.data_age_seconds is not None else None
        ),
        "reference_time": iso_utc(reference_time) if reference_time else None,
        "reference_price": quality.reference_price,
        "horizon_minutes": horizon,
        "horizon_group": horizon_group(horizon),
        "expiry_time": iso_utc(expiry) if expiry else None,
        "signal": decision.signal,
        "call_score": scores.call_score,
        "put_score": scores.put_score,
        "directional_score": scores.directional_score,
        "confidence_score": scores.confidence_score,
        "data_quality_score": quality.data_quality_score,
        "signal_validity": decision.signal_validity,
        "signal_strength": decision.signal_strength,
        "contradiction_ratio": scores.contradiction_ratio,
        "feature_coverage": scores.feature_coverage,
        "vol_regime_factor": scores.vol_regime_factor,
        "primary_drivers": list(scores.primary_drivers),
        "primary_conflicts": list(scores.primary_conflicts),
        "warning_flags": sorted(set(decision.warning_flags)),
        "no_trade_reason": decision.no_trade_reason,
        "family_contributions": dict(scores.contributions),
        "missing_families": dict(scores.missing_families),
        "feature_values": dict(feature_values),
        "missing_candle_count": quality.missing_count,
        "duplicate_candle_count": quality.duplicate_count,
        "payout_ratio": payout_ratio,
        "break_even_win_probability": break_even,
        # Always None in this milestone; the output validator errors otherwise.
        "calibrated_probability": None,
        "expected_edge": None,
        "run_status": None,  # filled after run-status assessment
    }


def _rank_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic ordering: actionable signals ranked first (confidence
    desc, |directional| desc, symbol asc, horizon asc), then NO_TRADE rows
    (symbol asc, horizon asc). Ties always break the same way."""
    actionable = [r for r in rows if r["signal"] != SIGNAL_NO_TRADE]
    no_trade = [r for r in rows if r["signal"] == SIGNAL_NO_TRADE]
    actionable.sort(key=lambda r: (
        -float(r["confidence_score"] or 0.0),
        -abs(float(r["directional_score"] or 0.0)),
        str(r["symbol"]),
        int(r["horizon_minutes"]),
    ))
    no_trade.sort(key=lambda r: (str(r["symbol"]), int(r["horizon_minutes"])))
    return actionable + no_trade


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def _write_outputs(
    cfg, rows, stages, assessment, universe, series_by_symbol,
    quality_by_symbol, health_summary, session, run_id, generated_at,
    payout_ratio, break_even,
) -> Dict[str, str]:
    out_dir = ensure_dir(cfg.run.output_dir)
    processed_dir = ensure_dir(cfg.run.processed_dir)
    paths: Dict[str, str] = {}

    # CSV (list/dict fields serialized for flat storage)
    csv_rows = []
    for r in rows:
        c = dict(r)
        for k in ("primary_drivers", "primary_conflicts", "warning_flags"):
            c[k] = "; ".join(c[k]) if c[k] else ""
        for k in ("family_contributions", "missing_families", "feature_values"):
            c[k] = "; ".join(f"{a}={b}" for a, b in c[k].items()) if c[k] else ""
        csv_rows.append(c)
    csv_path = processed_dir / "binary_options_candidates.csv"
    write_csv(csv_path, pd.DataFrame(csv_rows))
    paths["candidates_csv"] = str(csv_path)

    # Markdown report
    md_path = out_dir / "binary_options_candidates.md"
    md_path.write_text(
        _render_report_md(cfg, rows, assessment, generated_at, payout_ratio, break_even),
        encoding="utf-8",
    )
    paths["candidates_md"] = str(md_path)

    # JSON summary
    signal_counts: Dict[str, int] = {}
    reason_counts: Dict[str, int] = {}
    for r in rows:
        signal_counts[r["signal"]] = signal_counts.get(r["signal"], 0) + 1
        if r["no_trade_reason"]:
            reason_counts[r["no_trade_reason"]] = reason_counts.get(r["no_trade_reason"], 0) + 1
    summary = {
        "run_id": run_id,
        "generated_at": generated_at,
        "module": "binary_options_selection",
        "run_status": assessment.run_status,
        "run_assessment": assessment.to_dict(),
        "market_state": session.state,
        "exchange_local_time": session.exchange_local_time,
        "timezone": session.timezone,
        "universe": {
            "mode": universe.mode,
            "symbols": universe.symbols,
            "dropped": universe.dropped,
            "notes": universe.notes,
            "error": universe.error,
        },
        "horizons_minutes": cfg.horizons.minutes,
        "interval": cfg.candles.interval,
        "provider": cfg.providers.intraday,
        "signal_counts": signal_counts,
        "no_trade_reason_counts": reason_counts,
        "payout": {
            "payout_ratio": payout_ratio,
            "break_even_win_probability": break_even,
            "note": (
                "Break-even is contextual only. Confidence scores are heuristics, "
                "not calibrated probabilities; profitability cannot be inferred "
                "without calibrated probabilities and actual contract terms."
            ),
        },
        "stages": [s.to_dict() for s in stages],
        "candidates": rows,
    }
    summary_path = out_dir / "binary_options_summary.json"
    write_json(summary_path, summary)
    paths["summary_json"] = str(summary_path)

    # Provider diagnostics
    diag = {
        "run_id": run_id,
        "generated_at": generated_at,
        "provider": cfg.providers.intraday,
        "health_check": health_summary.to_dict() if health_summary else None,
        "symbols": [
            {
                **series_by_symbol[sym].to_meta(),
                "data_quality_score": (
                    quality_by_symbol[sym].data_quality_score
                    if sym in quality_by_symbol else None
                ),
                "stale": (
                    quality_by_symbol[sym].stale if sym in quality_by_symbol else None
                ),
                "usable": (
                    quality_by_symbol[sym].usable if sym in quality_by_symbol else None
                ),
            }
            for sym in sorted(series_by_symbol)
        ],
    }
    diag_json = out_dir / "binary_options_provider_diagnostics.json"
    write_json(diag_json, diag)
    paths["diagnostics_json"] = str(diag_json)
    diag_md = out_dir / "binary_options_provider_diagnostics.md"
    diag_md.write_text(_render_diagnostics_md(diag), encoding="utf-8")
    paths["diagnostics_md"] = str(diag_md)
    return paths


def _render_report_md(cfg, rows, assessment, generated_at, payout_ratio, break_even) -> str:
    lines: List[str] = ["# Binary options selection — research signals", ""]
    lines.append(render_status_banner(assessment, generated_at))
    lines.append("")
    lines.append("## How to read this report")
    lines.append("")
    lines.append(
        "- Each row is one **underlying x expiration horizon** with its own "
        "features and score; a 5-minute and a 60-minute signal on the same "
        "symbol are independent answers."
    )
    lines.append(
        "- `CALL` = the weighted intraday evidence points to a settlement above "
        "the reference price; `PUT` = below it; `NO_TRADE` = the evidence is too "
        "close, too weak, contradictory, stale, or the data/session is unsuitable. "
        "NO_TRADE is a first-class answer, not an error."
    )
    lines.append(
        "- `confidence_score` (0-1) is a bounded **heuristic** combining signal "
        "margin, contradiction, data quality, feature coverage, and the "
        "volatility regime. It is **not a calibrated probability** and must not "
        "be treated as a win rate."
    )
    if payout_ratio is not None:
        lines.append(
            f"- Configured payout ratio {payout_ratio}: break-even win probability "
            f"= 1 / (1 + {payout_ratio}) = **{break_even:.1%}** — context only; "
            "profitability cannot be inferred from heuristic scores."
        )
    lines.append(
        "- `data_age` is the age of the latest completed candle at generation "
        "time; stale rows are never presented as live signals."
    )
    lines.append("")

    actionable = [r for r in rows if r["signal"] != SIGNAL_NO_TRADE]
    no_trade = [r for r in rows if r["signal"] == SIGNAL_NO_TRADE]

    lines.append(f"## Ranked signals ({len(actionable)})")
    lines.append("")
    if actionable:
        top = actionable[: cfg.run.top_n]
        header = (
            "| # | symbol | horizon | signal | strength | validity | dir | call | put "
            "| conf | quality | age (s) | drivers | conflicts | warnings |"
        )
        lines.append(header)
        lines.append("|" + "---|" * 15)
        for i, r in enumerate(top, 1):
            lines.append(
                f"| {i} | {r['symbol']} | {r['horizon_minutes']}m "
                f"({r['horizon_group']}) | **{r['signal']}** | {r['signal_strength']} "
                f"| {r['signal_validity']} | {r['directional_score']:+.1f} "
                f"| {r['call_score']:.1f} | {r['put_score']:.1f} "
                f"| {r['confidence_score']:.2f} | {r['data_quality_score']:.0f} "
                f"| {r['data_age_seconds'] if r['data_age_seconds'] is not None else '—'} "
                f"| {', '.join(r['primary_drivers']) or '—'} "
                f"| {', '.join(r['primary_conflicts']) or '—'} "
                f"| {', '.join(r['warning_flags']) or '—'} |"
            )
    else:
        lines.append(
            "_No actionable signals. This is a normal outcome — see the NO_TRADE "
            "breakdown below for the reasons._"
        )
    lines.append("")

    lines.append(f"## NO_TRADE rows ({len(no_trade)})")
    lines.append("")
    reason_counts: Dict[str, int] = {}
    for r in no_trade:
        reason_counts[r["no_trade_reason"]] = reason_counts.get(r["no_trade_reason"], 0) + 1
    if reason_counts:
        lines.append("| reason | rows |")
        lines.append("|---|---|")
        for reason, n in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {reason} | {n} |")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append("---")
    lines.append(
        "> **Disclaimer.** Research and educational output only. Not financial "
        "advice. Binary options are high-risk instruments and many platforms "
        "operate under weak regulation; nothing here executes trades, sizes "
        "stakes, or promises any outcome. Past intraday patterns do not "
        "guarantee future results."
    )
    lines.append("")
    return "\n".join(lines)


def _render_diagnostics_md(diag: Dict[str, Any]) -> str:
    lines = [
        "# Binary options provider diagnostics",
        "",
        f"Run `{diag['run_id']}` — provider **{diag['provider']}** — {diag['generated_at']}",
        "",
    ]
    hc = diag.get("health_check")
    if hc:
        lines.append(
            f"Health probe: {hc['ok_count']} ok / {hc['fail_count']} failed"
            + (" — **SYSTEMIC FAILURE**" if hc["systemic_failure"] else "")
        )
        lines.append("")
    lines.append(
        "| symbol | status | error_type | source | candles | latest close | "
        "quality | stale | usable |"
    )
    lines.append("|" + "---|" * 9)
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
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="binary-options-selection",
        description=(
            "Binary options candidate + directional selection (research signals "
            "only: CALL / PUT / NO_TRADE per asset and 5-120 minute horizon; "
            "no execution, no broker, no probabilities)."
        ),
    )
    p.add_argument("--config", default=None, help="Path to YAML config "
                   "(default configs/binary_options/default.yaml).")
    p.add_argument("--tickers", nargs="+", default=None, metavar="SYM",
                   help="Explicit underlyings; implies --universe custom.")
    p.add_argument("--watchlist", default=None, metavar="PATH",
                   help="Watchlist file (one symbol per line); implies --universe watchlist.")
    p.add_argument("--universe", choices=["custom", "watchlist", "asset-selection"],
                   default=None, help="Universe mode (default from YAML).")
    p.add_argument("--asset-selection-input", default=None, metavar="CSV",
                   help="Path to an asset_selection_results.csv used as the universe "
                        "(quality filter only, not an intraday predictor).")
    p.add_argument("--horizons", nargs="+", type=int, default=None, metavar="MIN",
                   help="Expiration horizons in minutes (5-120, multiples of the interval).")
    p.add_argument("--interval", default=None,
                   help=f"Candle interval; one of {sorted(SUPPORTED_INTERVALS)}.")
    p.add_argument("--top", type=int, default=None, help="Top-N rows in the Markdown report.")
    p.add_argument("--provider", default=None,
                   help="Intraday provider name (default from YAML; 'yfinance').")
    p.add_argument("--refresh-cache", action="store_true",
                   help="Invalidate the intraday cache before running.")
    p.add_argument("--no-cache", action="store_true", help="Disable the cache entirely.")
    p.add_argument("--output-dir", default=None, help="Override the reports directory.")
    p.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR.")
    p.add_argument("--health-check-only", action="store_true",
                   help="Probe the intraday provider on the benchmark symbols, write "
                        "reports/binary_options_provider_health.json, and exit "
                        "(2 on systemic failure).")
    p.add_argument("--allow-extended-hours", action="store_true",
                   help="Analyze pre-market and after-hours candles "
                        "(sets session=extended with both windows allowed).")
    partial = p.add_mutually_exclusive_group()
    partial.add_argument("--allow-partial-results", dest="allow_partial",
                         action="store_true", default=None,
                         help="Present a run with some failed symbols as PARTIAL (default).")
    partial.add_argument("--no-partial-results", dest="allow_partial",
                         action="store_false",
                         help="Any failed symbol degrades the run instead.")
    return p


def _apply_cli_overrides(cfg: BinaryOptionsConfig, args: argparse.Namespace) -> List[str]:
    """Mutate cfg from CLI args; returns re-validation problems (empty = ok)."""
    if args.tickers:
        cfg.universe.mode = "custom"
        cfg.universe.tickers = args.tickers
    if args.watchlist:
        cfg.universe.mode = "watchlist"
        cfg.universe.watchlist_file = args.watchlist
    if args.universe:
        cfg.universe.mode = args.universe
    if args.asset_selection_input:
        cfg.universe.asset_selection_input = args.asset_selection_input
        if not args.universe and not args.tickers and not args.watchlist:
            cfg.universe.mode = "asset-selection"
    if args.horizons:
        cfg.horizons.minutes = args.horizons
    if args.interval:
        cfg.candles.interval = args.interval
    if args.top is not None:
        cfg.run.top_n = args.top
    if args.provider:
        cfg.providers.intraday = args.provider
    if args.output_dir:
        cfg.run.output_dir = args.output_dir
    if args.no_cache:
        cfg.cache.enabled = False
    if args.allow_extended_hours:
        cfg.market_session.session = "extended"
        cfg.market_session.allow_premarket = True
        cfg.market_session.allow_afterhours = True
    if args.allow_partial is not None:
        cfg.robustness.allow_partial_results = args.allow_partial
    return validate_config(cfg)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (ConfigValidationError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    problems = _apply_cli_overrides(cfg, args)
    if problems:
        print("Invalid arguments/configuration:", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        return 2
    _configure_logging(args.log_level or cfg.logging.level)

    interval_seconds = SUPPORTED_INTERVALS[cfg.candles.interval] * 60
    cache = Cache(
        directory=cfg.cache.dir, enabled=cfg.cache.enabled,
        default_ttl=cfg.cache.ttl_for_interval(interval_seconds),
    )
    if args.refresh_cache:
        removed = cache.invalidate()
        logger.info("Cache invalidated (%d entries removed)", removed)
    limiter = RateLimiter(cfg.rate_limits.get(cfg.providers.intraday, 0.0))
    try:
        provider = build_intraday_provider(cfg.providers.intraday, cache, limiter)
    except ValueError as exc:
        print(f"Provider error: {exc}", file=sys.stderr)
        return 2

    if args.health_check_only:
        summary = run_intraday_health_check(provider, cfg)
        out_dir = ensure_dir(cfg.run.output_dir)
        path = out_dir / "binary_options_provider_health.json"
        write_json(path, summary.to_dict())
        print(f"Health check written to {path} "
              f"({summary.ok_count} ok / {summary.fail_count} failed)")
        return 2 if summary.systemic_failure else 0

    result = run_pipeline(
        cfg,
        cli_tickers=args.tickers,
        cli_watchlist_file=args.watchlist,
        provider=provider,
    )
    assessment = result["assessment"]
    print(f"Run {result['run_id']}: status={assessment.run_status}; "
          f"{assessment.rows_produced} rows "
          f"({sum(1 for r in result['rows'] if r['signal'] != SIGNAL_NO_TRADE)} actionable). "
          f"Reports in {cfg.run.output_dir}/")
    for reason in assessment.reasons:
        print(f"  - {reason}")
    return 2 if assessment.run_status == RUN_INVALID else 0


if __name__ == "__main__":
    sys.exit(main())
