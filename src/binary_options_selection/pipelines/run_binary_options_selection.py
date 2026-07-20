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

from trading_common.cache import Cache
from trading_common.io import ensure_dir, write_json
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
from ..catalog.context import build_contract_context, unknown_contract_context
from ..catalog.providers import CatalogError, build_catalog_provider
from ..catalog.validation import validate_catalog
from ..data_providers.base import IntradayCandlesProvider, IntradaySeries
from ..features.engine import compute_features
from ..health.provider_health import run_intraday_health_check
from ..market_session import classify_session, classify_session_for_asset, expiry_for
from ..scoring.directional import DirectionalScores, score_direction
from ..signals.decision import (
    SIGNAL_NO_TRADE,
    LiquidityAssessment,
    assess_liquidity,
    decide_signal,
)
from .. import reporting
from ..signals.payout import break_even_win_probability
from ..universe import UniverseResult, resolve_universe
from ..utils.timeutils import iso_utc, utc_now
from ..validation.candles import SeriesQuality, assess_series
from ..validation.output_validation import render_markdown as render_validation_md
from ..validation.output_validation import validate_outputs
from ..validation.run_status import (
    RUN_INVALID,
    determine_run_status,
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
# Instrument catalog resolution
# ---------------------------------------------------------------------------

@dataclass
class _CatalogBundle:
    """What Stage 1 learned from the instrument catalog (possibly nothing)."""
    snapshot: Any = None                       # CatalogSnapshot | None
    validation: Any = None                     # CatalogValidationResult | None
    instrument_by_symbol: Dict[str, Any] = field(default_factory=dict)
    stale: bool = False
    notes: List[str] = field(default_factory=list)
    research_note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.research_note:
            self.notes.append(self.research_note)


def _resolve_catalog_universe(cfg, now) -> "tuple[UniverseResult, _CatalogBundle]":
    """Resolve the universe from the verified instrument catalog.

    Without a usable catalog the pipeline does NOT invent availability: it
    falls back to the configured watchlist purely so that every requested
    symbol still gets explainable NO_TRADE (INSTRUMENT_AVAILABILITY_UNKNOWN)
    rows, and the run is degraded.
    """
    bundle = _CatalogBundle()
    ic = cfg.instrument_catalog
    universe = UniverseResult(mode="catalog")

    if not ic.path:
        bundle.notes.append(
            "no instrument catalog configured (instrument_catalog.path) -- "
            "contract availability is unknown for every symbol"
        )
        universe.symbols = list(cfg.universe.watchlist)[: cfg.universe.max_symbols]
        universe.notes.append(
            "watchlist used only to enumerate availability-unknown rows"
        )
        if not universe.symbols:
            universe.error = (
                "no instrument catalog and no watchlist symbols to report on"
            )
        return universe, bundle

    try:
        provider = build_catalog_provider(ic.path, ic.provider)
        snapshot = provider.get_available_instruments(as_of=now, platform=ic.platform)
    except CatalogError as exc:
        bundle.notes.append(f"instrument catalog unusable: {exc}")
        universe.error = f"instrument catalog unusable: {exc}"
        return universe, bundle

    result = validate_catalog(snapshot, ic, now=now)
    bundle.snapshot = snapshot
    bundle.validation = result
    bundle.stale = result.stale
    if not result.ok:
        bundle.notes.extend(f"catalog problem: {p}" for p in result.problems)
        universe.error = "instrument catalog failed validation: " + "; ".join(
            result.problems[:3]
        )
        return universe, bundle
    bundle.notes.extend(f"catalog warning: {w}" for w in result.warnings)

    instruments = list(snapshot.instruments)
    if ic.asset_types:
        kept = [i for i in instruments if i.asset_type in ic.asset_types]
        for i in instruments:
            if i not in kept:
                universe.dropped[i.underlying_symbol] = "asset_type_filtered"
        instruments = kept
    for inst in instruments:
        issues = result.instrument_issues.get(inst.platform_asset_id)
        if issues:
            universe.dropped[inst.underlying_symbol] = "catalog_issues: " + "; ".join(issues)
            continue
        if inst.underlying_symbol in bundle.instrument_by_symbol:
            universe.dropped[inst.underlying_symbol] = "duplicate_underlying_in_catalog"
            continue
        bundle.instrument_by_symbol[inst.underlying_symbol] = inst
        universe.symbols.append(inst.underlying_symbol)
        if inst.underlying_name:
            universe.name_by_symbol[inst.underlying_symbol] = inst.underlying_name
    universe.symbols = universe.symbols[: cfg.universe.max_symbols]
    universe.notes.append(
        f"catalog: {snapshot.provider} platform={snapshot.platform} "
        f"instruments={len(universe.symbols)} stale={result.stale}"
    )
    if not universe.symbols:
        universe.error = "catalog contained no usable instruments"
    return universe, bundle


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
    config_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the staged pipeline. ``provider`` and ``now`` are injectable so
    tests run deterministically offline; ``config_path`` feeds the manifest."""
    now = now or utc_now()
    run_id = uuid.uuid4().hex[:12]
    generated_at = iso_utc(now)
    stages: List[StageStats] = []
    interval = cfg.candles.interval
    interval_minutes = SUPPORTED_INTERVALS[interval]
    interval_seconds = interval_minutes * 60

    # --- Stage 1: universe / instrument catalog ------------------------------
    # Non-catalog modes analyze generic symbols; config validation only allows
    # them together with run.research_mode, so mode != "catalog" implies an
    # explicit research run.
    research_mode = cfg.run.research_mode or cfg.universe.mode != "catalog"
    s1 = StageStats("1_universe")
    with _Timer(s1):
        if cfg.universe.mode == "catalog":
            universe, catalog = _resolve_catalog_universe(cfg, now)
        else:
            universe = resolve_universe(
                cfg, cli_tickers=cli_tickers, cli_watchlist_file=cli_watchlist_file
            )
            catalog = _CatalogBundle(research_note="research mode: no contract catalog in play")
        s1.input_count = len(universe.symbols) + len(universe.dropped)
        s1.output_count = len(universe.symbols)
        for sym, reason in universe.dropped.items():
            s1.drop(reason)
        s1.notes.extend(universe.notes)
        if catalog.notes:
            s1.notes.extend(catalog.notes)
        if universe.error:
            s1.notes.append(universe.error)
    stages.append(s1)
    logger.info("Stage 1 universe: %d symbols (%s mode, research=%s)%s",
                len(universe.symbols), universe.mode, research_mode,
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
        market_delay = float(
            cfg.providers.known_delay_seconds.get(getattr(provider, "name", ""), 0.0)
        )
        for sym in universe.symbols:
            series = series_by_symbol.get(sym)
            quality = quality_by_symbol.get(sym) or SeriesQuality(
                symbol=sym, error_type=err.NO_INTRADAY_DATA
            )
            instrument = catalog.instrument_by_symbol.get(sym)
            asset_type = instrument.asset_type if instrument else "equity"
            row_session = classify_session_for_asset(now, cfg.market_session, asset_type)
            # Volume-based liquidity gates only where the feed carries real
            # volume; other asset types skip them with a visible warning.
            if asset_type in cfg.liquidity.enforce_for_asset_types:
                liquidity = liquidity_by_symbol.get(sym) or assess_liquidity([], cfg)
                liq_warning = None
            else:
                liquidity = LiquidityAssessment(ok=True, reasons=[])
                liq_warning = "VOLUME_DATA_UNRELIABLE"
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
                contract_ctx = (
                    build_contract_context(
                        instrument,
                        snapshot=catalog.snapshot,
                        catalog_stale=catalog.stale,
                        horizon_minutes=horizon,
                        cfg=cfg.instrument_catalog,
                        research_mode=research_mode,
                        market_reference_price=quality.reference_price,
                        now=now,
                    )
                    if catalog.snapshot is not None and instrument is not None
                    else unknown_contract_context(research_mode=research_mode)
                )
                decision = decide_signal(
                    scores, quality, row_session, liquidity,
                    expiry_utc=expiry, cfg=cfg,
                    contract=contract_ctx,
                    market_delay_seconds=market_delay,
                    abnormal_market="ABNORMAL_RANGE" in vol_flags,
                    asset_type=asset_type,
                )
                decision.warning_flags.extend(vol_flags)
                if liq_warning:
                    decision.warning_flags.append(liq_warning)
                rows.append(_build_row(
                    run_id=run_id, generated_at=generated_at, symbol=sym,
                    company=universe.name_by_symbol.get(sym),
                    series=series, quality=quality, scores=scores,
                    decision=decision, session_state=row_session.state,
                    interval=interval, reference_time=reference_time,
                    expiry=expiry if reference_time is not None else None,
                    horizon=horizon, payout_ratio=payout_ratio,
                    break_even=break_even, timezone_name=cfg.market_session.timezone,
                    feature_values=feature_values,
                    contract=contract_ctx, instrument=instrument,
                    market_delay_seconds=market_delay, now=now,
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
        run_dir = ensure_dir(reporting.run_dir_for(cfg, run_id))
        paths = reporting.write_signal_csvs(cfg, rows)
        catalog_notes = list(catalog.notes)
        for name, subset, title, with_matrix in (
            ("signals_all", rows, "Binary options signals — all horizons", True),
            ("signals_short_term",
             [r for r in rows if r["horizon_group"] == "short_term"],
             "Binary options signals — short term (5-15 min)", False),
            ("signals_extended_intraday",
             [r for r in rows if r["horizon_group"] == "extended_intraday"],
             "Binary options signals — extended intraday (30-120 min)", False),
        ):
            md = reporting.render_signals_md(
                cfg, subset, assessment, generated_at, title=title,
                horizons=cfg.horizons.minutes, catalog_notes=catalog_notes,
                include_matrix=with_matrix,
            )
            (run_dir / f"{name}.md").write_text(md, encoding="utf-8")
            paths[f"{name}_md"] = str(run_dir / f"{name}.md")

        signal_counts: Dict[str, int] = {}
        reason_counts: Dict[str, int] = {}
        for r in rows:
            signal_counts[r["signal"]] = signal_counts.get(r["signal"], 0) + 1
            if r["no_trade_reason"]:
                reason_counts[r["no_trade_reason"]] = (
                    reason_counts.get(r["no_trade_reason"], 0) + 1
                )
        summary = {
            "run_id": run_id,
            "generated_at": generated_at,
            "module": "binary_options_selection",
            "run_status": assessment.run_status,
            "run_assessment": assessment.to_dict(),
            "market_state": session.state,
            "exchange_local_time": session.exchange_local_time,
            "timezone": session.timezone,
            "research_mode": research_mode,
            "universe": {
                "mode": universe.mode,
                "symbols": universe.symbols,
                "dropped": universe.dropped,
                "notes": universe.notes,
                "error": universe.error,
            },
            "catalog": {
                "provider": catalog.snapshot.provider if catalog.snapshot else None,
                "platform": catalog.snapshot.platform if catalog.snapshot else None,
                "path": catalog.snapshot.path if catalog.snapshot else None,
                "instruments": len(catalog.instrument_by_symbol),
                "stale": catalog.stale,
                "age_seconds": (
                    catalog.snapshot.age_seconds(now) if catalog.snapshot else None
                ),
                "notes": catalog.notes,
            },
            "horizons_minutes": cfg.horizons.minutes,
            "horizon_matrix": reporting.horizon_matrix(rows, cfg.horizons.minutes),
            "interval": cfg.candles.interval,
            "market_data_provider": cfg.providers.intraday,
            "signal_counts": signal_counts,
            "no_trade_reason_counts": reason_counts,
            "stages": [st.to_dict() for st in stages],
            "candidates": rows,
        }
        write_json(run_dir / "run_summary.json", summary)
        paths["run_summary_json"] = str(run_dir / "run_summary.json")

        diag = {
            "run_id": run_id,
            "generated_at": generated_at,
            "market_data_provider": cfg.providers.intraday,
            "catalog_provider": catalog.snapshot.provider if catalog.snapshot else None,
            "health_check": health_summary.to_dict() if health_summary else None,
            "catalog": summary["catalog"],
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
        paths.update(reporting.write_json_and_md(
            run_dir, "provider_diagnostics", diag, reporting.render_diagnostics_md(diag)
        ))
        s9.output_count = len(paths)
    stages.append(s9)

    # --- Stage 10: output validation ------------------------------------------
    validation_result = None
    if cfg.validation.enabled:
        validation_result = validate_outputs(rows, run_status=assessment.run_status)
        paths.update(reporting.write_json_and_md(
            run_dir, "output_validation", validation_result,
            render_validation_md(validation_result),
        ))
        logger.info("Output validation: %s", validation_result["overall_status"])

    # --- Run manifest + latest/ refresh ---------------------------------------
    finished = utc_now()
    manifest = reporting.build_run_manifest(
        cfg=cfg,
        run_id=run_id,
        started_at=generated_at,
        finished_at=iso_utc(finished),
        duration_seconds=(finished - now).total_seconds(),
        config_path=config_path,
        catalog_path=cfg.instrument_catalog.path,
        catalog_hash=catalog.snapshot.catalog_hash if catalog.snapshot else None,
        market_data_provider=cfg.providers.intraday,
        catalog_provider=catalog.snapshot.provider if catalog.snapshot else None,
        output_paths=paths,
        run_status=assessment.run_status,
    )
    write_json(run_dir / "run_manifest.json", manifest)
    paths["run_manifest_json"] = str(run_dir / "run_manifest.json")
    paths["latest_dir"] = reporting.refresh_latest(cfg, run_id)
    paths["run_dir"] = str(run_dir)

    return {
        "run_id": run_id,
        "rows": rows,
        "assessment": assessment,
        "stages": stages,
        "paths": paths,
        "validation": validation_result,
        "session": session,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Row construction / ranking
# ---------------------------------------------------------------------------

def _build_row(
    *, run_id, generated_at, symbol, company, series, quality, scores,
    decision, session_state, interval, reference_time, expiry, horizon,
    payout_ratio, break_even, timezone_name, feature_values,
    contract=None, instrument=None, market_delay_seconds=0.0, now=None,
) -> Dict[str, Any]:
    # Contract-specific economics take precedence over the research-level
    # global payout assumption; without a contract payout, break-even stays
    # None rather than being fabricated from the config default.
    row_payout = contract.payout_ratio if contract and contract.payout_ratio is not None else payout_ratio
    row_break_even = (
        round(break_even_win_probability(row_payout), 4)
        if row_payout is not None else None
    )
    data_age = (
        round(quality.data_age_seconds, 1)
        if quality.data_age_seconds is not None else None
    )
    time_remaining = (
        round(horizon * 60.0 - quality.data_age_seconds, 1)
        if quality.data_age_seconds is not None else None
    )
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "analysis_timestamp": generated_at,
        # --- underlying / contract identity ---
        "symbol": symbol,                      # kept for compatibility
        "underlying_symbol": symbol,
        "underlying_name": (
            (instrument.underlying_name if instrument else None) or company
        ),
        "company_name": company,
        "asset_type": instrument.asset_type if instrument else "equity",
        "market": instrument.market if instrument else None,
        "is_otc": bool(instrument.is_otc) if instrument else False,
        "platform": instrument.platform if instrument else None,
        "platform_asset_id": instrument.platform_asset_id if instrument else None,
        "platform_asset_name": instrument.platform_asset_name if instrument else None,
        "contract_id": instrument.contract_id if instrument else None,
        "contract_status": instrument.contract_status if instrument else None,
        "contract_available": bool(contract.contract_available) if contract else False,
        "availability_known": bool(contract.availability_known) if contract else False,
        # --- providers / provenance ---
        "provider_symbol": (
            (instrument.provider_symbol if instrument else None)
            or (getattr(series, "provider_symbol", None) if series else None)
        ),
        "provider": getattr(series, "provider", None) if series else None,
        "market_data_provider": getattr(series, "provider", None) if series else None,
        "provider_status": getattr(series, "status", "error") if series else "error",
        "data_source": getattr(series, "data_source", None) if series else None,
        "known_provider_delay_seconds": market_delay_seconds,
        "catalog_provider": contract.catalog_provider if contract else None,
        "catalog_age_seconds": (
            round(contract.catalog_age_seconds, 1)
            if contract and contract.catalog_age_seconds is not None else None
        ),
        # --- session / timing ---
        "market_state": session_state,
        "timezone": timezone_name,
        "interval": interval,
        "latest_candle_close": (
            iso_utc(quality.latest_complete_close)
            if quality.latest_complete_close else None
        ),
        "latest_completed_candle_timestamp": (
            iso_utc(quality.latest_complete_close)
            if quality.latest_complete_close else None
        ),
        "data_age_seconds": data_age,
        "market_data_age_seconds": data_age,
        "reference_time": iso_utc(reference_time) if reference_time else None,
        "reference_timestamp": iso_utc(reference_time) if reference_time else None,
        "reference_price": quality.reference_price,
        "platform_reference_price": (
            contract.platform_reference_price if contract else None
        ),
        "platform_quote_age_seconds": (
            round(contract.platform_quote_age_seconds, 1)
            if contract and contract.platform_quote_age_seconds is not None else None
        ),
        "absolute_quote_difference": (
            contract.absolute_quote_difference if contract else None
        ),
        "relative_quote_difference_bps": (
            contract.relative_quote_difference_bps if contract else None
        ),
        "quote_alignment_status": (
            contract.quote_alignment_status if contract else "unavailable"
        ),
        "horizon_minutes": horizon,
        "horizon_group": horizon_group(horizon),
        "expiry_time": iso_utc(expiry) if expiry else None,
        "expiration_timestamp": iso_utc(expiry) if expiry else None,
        "time_remaining_seconds": time_remaining,
        # --- decision ---
        "recommended_action": decision.recommended_action,
        "signal": decision.signal,
        "call_score": scores.call_score,
        "put_score": scores.put_score,
        "directional_score": scores.directional_score,
        "confidence_score": scores.confidence_score,
        "data_quality_score": quality.data_quality_score,
        "liquidity_score": _liquidity_score(quality),
        "signal_validity": decision.signal_validity,
        "signal_strength": decision.signal_strength,
        "ranking_validity": (
            "actionable" if decision.signal != SIGNAL_NO_TRADE else "excluded"
        ),
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
        # --- contract economics (payout ratio = net profit per unit staked) ---
        "payout_ratio": row_payout,
        "payout_source": (
            contract.payout_source
            if contract and contract.payout_ratio is not None
            else ("config" if payout_ratio is not None else None)
        ),
        "payout_timestamp": contract.payout_timestamp if contract else None,
        "break_even_win_probability": row_break_even,
        # Always None in this milestone; the output validator errors otherwise.
        "calibrated_probability": None,
        "expected_edge": None,
        "run_status": None,  # filled after run-status assessment
    }


def _liquidity_score(quality: SeriesQuality) -> float:
    """Bounded 0-100 liquidity indicator: penalizes zero-volume bars only.

    A fuller dollar-volume percentile model needs cross-sectional context this
    per-symbol pipeline does not have; the honest bounded proxy is the share
    of bars that actually traded."""
    return round(100.0 * (1.0 - min(1.0, quality.zero_volume_ratio)), 1)


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
    p.add_argument("--provider", "--market-data-provider", dest="provider",
                   default=None,
                   help="Intraday market-data provider (default from YAML; 'yfinance').")
    p.add_argument("--instrument-catalog", default=None, metavar="PATH",
                   help="Path to YOUR verified platform catalog (YAML/CSV); see "
                        "configs/binary_options/instruments.example.yaml.")
    p.add_argument("--catalog-provider", choices=["auto", "yaml", "csv"],
                   default=None, help="Catalog file format (default: auto by extension).")
    p.add_argument("--platform", default=None,
                   help="Only analyze catalog instruments for this platform.")
    p.add_argument("--asset-types", nargs="+", default=None, metavar="TYPE",
                   help="Only analyze these asset types (equity etf forex index "
                        "commodity cryptocurrency).")
    otc = p.add_mutually_exclusive_group()
    otc.add_argument("--exclude-otc", dest="exclude_otc", action="store_true",
                     default=None, help="Exclude OTC contracts (default).")
    otc.add_argument("--allow-otc", dest="exclude_otc", action="store_false",
                     help="Analyze OTC contracts too (rows carry an OTC warning).")
    p.add_argument("--research-mode", action="store_true",
                   help="Explicit research analysis: relaxes contract-availability "
                        "requirements (generic watchlists, missing payout data). "
                        "Rows are visibly marked; nothing becomes actionable "
                        "without a verified catalog.")
    p.add_argument("--validate-catalog-only", action="store_true",
                   help="Validate the instrument catalog schema/freshness, print "
                        "the report, and exit (2 on fatal problems).")
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
    if args.instrument_catalog:
        cfg.instrument_catalog.path = args.instrument_catalog
    if args.catalog_provider:
        cfg.instrument_catalog.provider = args.catalog_provider
    if args.platform:
        cfg.instrument_catalog.platform = args.platform
    if args.asset_types:
        cfg.instrument_catalog.asset_types = args.asset_types
    if args.exclude_otc is not None:
        cfg.instrument_catalog.exclude_otc = args.exclude_otc
    if args.research_mode:
        cfg.run.research_mode = True
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


def _validate_catalog_cli(cfg: BinaryOptionsConfig) -> int:
    """--validate-catalog-only: schema/freshness report, exit 2 on fatal problems."""
    ic = cfg.instrument_catalog
    if not ic.path:
        print("No instrument catalog configured. Pass --instrument-catalog PATH "
              "or set instrument_catalog.path in the YAML.", file=sys.stderr)
        return 2
    try:
        snapshot = build_catalog_provider(ic.path, ic.provider).get_available_instruments(
            platform=ic.platform
        )
    except CatalogError as exc:
        print(f"Catalog unusable: {exc}", file=sys.stderr)
        return 2
    result = validate_catalog(snapshot, ic, now=utc_now())
    print(f"Catalog: {ic.path}")
    print(f"  platform:    {snapshot.platform}")
    print(f"  instruments: {len(snapshot.instruments)}")
    age = f"{result.age_seconds:.0f}s" if result.age_seconds is not None else "unknown"
    print(f"  age:         {age} (stale: {result.stale})")
    for prob in result.problems:
        print(f"  PROBLEM: {prob}")
    for warn in result.warnings:
        print(f"  warning: {warn}")
    for aid, issues in result.instrument_issues.items():
        for issue in issues:
            print(f"  {aid}: {issue}")
    if result.ok:
        print("Catalog schema: OK")
        return 0
    print("Catalog schema: FAILED", file=sys.stderr)
    return 2


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

    if args.validate_catalog_only:
        return _validate_catalog_cli(cfg)

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
        config_path=args.config,
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
