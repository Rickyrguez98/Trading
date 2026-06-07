"""Data-coverage validation + ranking-validity gating.

This is the layer that answers the audit's hardest question: *should we present
this ranking at all?* A run that finished without crashing is NOT the same as a
run whose data is trustworthy. Here we measure how much of each data type we
actually obtained (coverage), how much of the shortfall was provider-side
(outage/blocked/rate-limited) versus honest "no data", and how many candidates
have real fundamentals behind them. From those measures plus the benchmark
health check we assign one of five ranking-validity verdicts:

    VALID_RANKING                  coverage healthy -> trust the ordering
    PARTIAL_RANKING_WITH_WARNINGS  usable but degraded -> ranking with caveats
    DIAGNOSTIC_ONLY                too degraded to recommend; emitted for triage
    INVALID_PROVIDER_FAILURE       benchmark mega-caps failed -> systemic outage
    INVALID_INSUFFICIENT_DATA      not enough valid candidates to rank honestly

News coverage NEVER blocks or downgrades to invalid -- a thin news tape only
lowers sentiment confidence (handled in stage 4) and raises a warning here.

The module is pure: it takes already-computed stage stats + the ranked frame +
config (+ optional health report) and returns plain dicts, so it is trivially
unit-testable without touching the network or the filesystem.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..config import AppConfig
from ..data_providers import errors as err

# --- Ranking-validity verdicts (improvement #5) ---
VALID_RANKING = "VALID_RANKING"
PARTIAL_RANKING_WITH_WARNINGS = "PARTIAL_RANKING_WITH_WARNINGS"
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
INVALID_PROVIDER_FAILURE = "INVALID_PROVIDER_FAILURE"
INVALID_INSUFFICIENT_DATA = "INVALID_INSUFFICIENT_DATA"

# --- Ranking-completeness status (materiality axis; audit fix) ---
# Orthogonal to ranking_validity: a run can be a VALID_RANKING by coverage yet
# still be incomplete because a *material* (mega-cap / benchmark / watchlist)
# ticker silently failed. We surface that here instead of letting a 99.8%
# headline bury a missing NVDA. A single critical miss downgrades completeness
# and is reported loudly, but does NOT by itself invalidate the whole run.
COMPLETE = "COMPLETE"
COMPLETE_WITH_MINOR_GAPS = "COMPLETE_WITH_MINOR_GAPS"
VALID_WITH_MATERIAL_WARNINGS = "VALID_WITH_MATERIAL_WARNINGS"
PARTIAL_CRITICAL_TICKER_FAILURE = "PARTIAL_CRITICAL_TICKER_FAILURE"
INVALID_SYSTEMIC_PROVIDER_FAILURE = "INVALID_SYSTEMIC_PROVIDER_FAILURE"

# --- Coarse run-status rollup (used for the report header + return code) ---
RUN_STATUS_VALID = "VALID"
RUN_STATUS_PARTIAL = "PARTIAL"
RUN_STATUS_DIAGNOSTIC = "DIAGNOSTIC"
RUN_STATUS_INVALID = "INVALID"

_VALIDITY_TO_RUN_STATUS = {
    VALID_RANKING: RUN_STATUS_VALID,
    PARTIAL_RANKING_WITH_WARNINGS: RUN_STATUS_PARTIAL,
    DIAGNOSTIC_ONLY: RUN_STATUS_DIAGNOSTIC,
    INVALID_PROVIDER_FAILURE: RUN_STATUS_INVALID,
    INVALID_INSUFFICIENT_DATA: RUN_STATUS_INVALID,
}

# Run statuses that should NOT be presented as an actionable ranking.
_NON_TRUSTED_STATUSES = {RUN_STATUS_DIAGNOSTIC, RUN_STATUS_INVALID}


def run_status_for(ranking_validity: str) -> str:
    return _VALIDITY_TO_RUN_STATUS.get(ranking_validity, RUN_STATUS_INVALID)


def is_trusted_run_status(run_status: str) -> bool:
    """A VALID or PARTIAL run may be presented as a ranking; others may not."""
    return run_status not in _NON_TRUSTED_STATUSES


def return_code_for(run_status: str) -> int:
    """0 for an actionable run (VALID/PARTIAL); 2 for diagnostic/invalid."""
    return 0 if is_trusted_run_status(run_status) else 2


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _stage_by_name(stage_stats: Sequence[Any], name: str) -> Optional[Any]:
    for s in stage_stats:
        if getattr(s, "name", None) == name:
            return s
    return None


def _provider_side_failures(stage: Any) -> int:
    """Count failures on ``stage`` whose error-type is a provider-side fault."""
    if stage is None:
        return 0
    fet = getattr(stage, "failure_error_types", {}) or {}
    return sum(v for k, v in fet.items() if err.is_provider_side(k))


def _coverage_block(stage: Any) -> Dict[str, Any]:
    """Per-data-type coverage from one stage's attempt/failure counters."""
    if stage is None:
        return {
            "attempted": 0,
            "with_data": 0,
            "failures": 0,
            "provider_side_failures": 0,
            "coverage_ratio": 0.0,
        }
    attempted = int(getattr(stage, "input_count", 0) or 0)
    failures = int(getattr(stage, "provider_failures", 0) or 0)
    with_data = max(0, attempted - failures)
    return {
        "attempted": attempted,
        "with_data": with_data,
        "failures": failures,
        "provider_side_failures": _provider_side_failures(stage),
        "coverage_ratio": round(_ratio(with_data, attempted), 4),
        "by_error_type": dict(getattr(stage, "failure_error_types", {}) or {}),
    }


# ---------------------------------------------------------------------------
# Coverage assessment
# ---------------------------------------------------------------------------

def _count_valid_candidates(ranked) -> int:
    """Ranked rows backed by real fundamentals (a present market cap).

    A row that survived to ranking but whose fundamentals came back empty
    (market_cap is None) is a thin candidate; we don't count it as "valid" for
    the minimum-candidates gate so an all-empty fundamentals run can't pass as a
    trustworthy ranking.
    """
    if ranked is None or getattr(ranked, "empty", True):
        return 0
    if "market_cap" not in ranked.columns:
        # No market-cap column at all -> treat every ranked row as thin.
        return 0
    import pandas as pd  # local import; pandas is already a hard dep

    mc = pd.to_numeric(ranked["market_cap"], errors="coerce")
    return int(mc.notna().sum())


def assess_coverage(
    stage_stats: Sequence[Any],
    ranked,
    config: AppConfig,
) -> Dict[str, Any]:
    """Measure price/fundamentals/news coverage and provider-side failure load.

    Returns the ``data_coverage_summary`` block embedded in the run summary.
    """
    rob = config.robustness
    price_cov = _coverage_block(_stage_by_name(stage_stats, "2_prices"))
    fund_cov = _coverage_block(_stage_by_name(stage_stats, "3_fundamentals"))
    news_cov = _coverage_block(_stage_by_name(stage_stats, "4_sentiment"))

    # Provider-failure ratio across the data types that gate a ranking
    # (price + fundamentals). News is advisory and excluded from the gate.
    gating_attempts = price_cov["attempted"] + fund_cov["attempted"]
    gating_provider_side = (
        price_cov["provider_side_failures"] + fund_cov["provider_side_failures"]
    )
    provider_failure_ratio = round(_ratio(gating_provider_side, gating_attempts), 4)

    n_ranked = 0 if (ranked is None or getattr(ranked, "empty", True)) else int(len(ranked))
    valid_candidates = _count_valid_candidates(ranked)

    return {
        "thresholds": {
            "min_price_coverage_ratio": rob.min_price_coverage_ratio,
            "min_fundamentals_coverage_ratio": rob.min_fundamentals_coverage_ratio,
            "min_news_coverage_ratio": rob.min_news_coverage_ratio,
            "max_provider_failure_ratio": rob.max_provider_failure_ratio,
            "min_valid_candidates_for_ranking": rob.min_valid_candidates_for_ranking,
        },
        "price": price_cov,
        "fundamentals": fund_cov,
        "news": news_cov,
        "provider_failure_ratio": provider_failure_ratio,
        "ranked_candidates": n_ranked,
        "valid_candidates": valid_candidates,
        "meets_price_coverage": price_cov["coverage_ratio"] >= rob.min_price_coverage_ratio,
        "meets_fundamentals_coverage": (
            fund_cov["coverage_ratio"] >= rob.min_fundamentals_coverage_ratio
        ),
        "meets_news_coverage": news_cov["coverage_ratio"] >= rob.min_news_coverage_ratio,
        "within_provider_failure_budget": (
            provider_failure_ratio <= rob.max_provider_failure_ratio
        ),
        "has_min_valid_candidates": (
            valid_candidates >= rob.min_valid_candidates_for_ranking
        ),
    }


# ---------------------------------------------------------------------------
# Materiality assessment (audit fix #4/#5)
# ---------------------------------------------------------------------------

def _ranked_tickers(ranked) -> set:
    if ranked is None or getattr(ranked, "empty", True) or "ticker" not in getattr(ranked, "columns", []):
        return set()
    return {str(t).strip().upper() for t in ranked["ticker"].tolist()}


def assess_materiality(
    stage_stats: Sequence[Any],
    ranked,
    config: AppConfig,
    *,
    health_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide whether the produced ranking is materially complete.

    Reads the ``material_gaps`` recorded by Stage 2 for critical tickers and
    buckets them (critical / large-cap / high-liquidity / user-watchlist), then
    assigns a ``ranking_completeness_status``. This is the layer that refuses to
    let overall coverage hide a missing mega-cap: every gap is named, and a
    failure on a hard-critical name downgrades completeness loudly.
    """
    ct = getattr(config, "critical_tickers", None)
    static_known = set()
    if ct is not None:
        static_known = {str(t).strip().upper() for t in (ct.static_tickers or [])}
        if getattr(ct, "treat_benchmark_as_critical", False):
            try:
                from ..health import BENCHMARK_TICKERS
                static_known |= {str(t).strip().upper() for t in BENCHMARK_TICKERS}
            except Exception:  # pragma: no cover - defensive
                pass
    watchlist = set()
    if ct is not None:
        watchlist = {str(t).strip().upper() for t in (ct.user_watchlist or [])}

    ranked_set = _ranked_tickers(ranked)

    gaps: List[Dict[str, Any]] = []
    for s in stage_stats:
        for g in getattr(s, "material_gaps", []) or []:
            gaps.append(g)

    # A gap is only "live" if the ticker did not otherwise make it into the
    # ranking (recovery via cache could have rescued it).
    live_gaps = [g for g in gaps if str(g.get("ticker", "")).strip().upper() not in ranked_set]

    critical_failures: List[str] = []
    large_cap_failures: List[str] = []
    high_liq_failures: List[str] = []
    watchlist_failures: List[str] = []
    confirmed_real: List[str] = []
    for g in live_gaps:
        t = str(g.get("ticker", "")).strip().upper()
        critical_failures.append(t)
        if g.get("is_large_cap"):
            large_cap_failures.append(t)
        if g.get("is_user_watchlist") or t in watchlist:
            watchlist_failures.append(t)
        # Known mega-caps + dynamic large-caps are treated as high-liquidity.
        if t in static_known or g.get("is_large_cap"):
            high_liq_failures.append(t)
        if g.get("company_confirmed_real"):
            confirmed_real.append(t)

    # Hard-critical = a statically configured mega-cap / benchmark name. A
    # watchlist-only or purely-dynamic miss is material but a step less severe.
    hard_critical = [t for t in critical_failures if t in static_known]

    blocking_systemic = bool((health_report or {}).get("any_blocking_systemic_failure"))

    # Were there non-material price misses at all (minor gaps)?
    price_stage = _stage_by_name(stage_stats, "2_prices")
    price_failures = int(getattr(price_stage, "provider_failures", 0) or 0) if price_stage else 0
    minor_gaps = max(0, price_failures - len(live_gaps))

    if blocking_systemic:
        completeness = INVALID_SYSTEMIC_PROVIDER_FAILURE
    elif hard_critical:
        completeness = PARTIAL_CRITICAL_TICKER_FAILURE
    elif live_gaps:
        completeness = VALID_WITH_MATERIAL_WARNINGS
    elif minor_gaps:
        completeness = COMPLETE_WITH_MINOR_GAPS
    else:
        completeness = COMPLETE

    return {
        "ranking_completeness_status": completeness,
        "critical_ticker_failures": sorted(set(critical_failures)),
        "hard_critical_ticker_failures": sorted(set(hard_critical)),
        "failed_large_cap_tickers": sorted(set(large_cap_failures)),
        "failed_high_liquidity_tickers": sorted(set(high_liq_failures)),
        "failed_user_watchlist_tickers": sorted(set(watchlist_failures)),
        "critical_failures_with_company_confirmed_real": sorted(set(confirmed_real)),
        "minor_non_material_price_gaps": minor_gaps,
        "material_data_gaps": live_gaps,
    }


# ---------------------------------------------------------------------------
# Ranking-validity verdict
# ---------------------------------------------------------------------------

def determine_run_status(
    coverage: Dict[str, Any],
    health_report: Optional[Dict[str, Any]],
    config: AppConfig,
    materiality: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Map coverage + benchmark health onto a ranking-validity verdict.

    Decision order (most severe first):
      1. Blocking systemic benchmark failure -> INVALID_PROVIDER_FAILURE
         (unless stop_on_systemic_provider_failure is off -> DIAGNOSTIC_ONLY).
      2. Too few valid candidates -> INVALID_INSUFFICIENT_DATA.
      3. All gating thresholds met -> VALID_RANKING.
      4. Otherwise degraded -> PARTIAL_RANKING_WITH_WARNINGS if partial runs are
         allowed, else DIAGNOSTIC_ONLY.
    """
    rob = config.robustness
    reasons: List[str] = []
    warnings: List[str] = []
    recommendations: List[str] = []

    blocking_systemic = bool(
        (health_report or {}).get("any_blocking_systemic_failure")
    )

    mat = materiality or {}

    # --- Materiality: a missing mega-cap must be reported loudly, even on an
    # otherwise-healthy run. Critical failures downgrade *completeness* (a
    # separate axis) and raise warnings; a single one does not invalidate the
    # whole run, but it must never be hidden behind aggregate coverage. ---
    crit_fail = mat.get("critical_ticker_failures") or []
    if crit_fail:
        confirmed = mat.get("critical_failures_with_company_confirmed_real") or []
        warnings.append(
            f"Material gap: {len(crit_fail)} critical/important ticker(s) "
            f"({', '.join(crit_fail)}) failed the price endpoint and are absent "
            "from the ranking. "
            + (
                f"Cross-provider fundamentals confirm {', '.join(confirmed)} still "
                "report data -> a price-provider coverage gap, NOT delisting. "
                if confirmed else ""
            )
            + "This is flagged as a material data gap rather than a silent drop."
        )
        recommendations.append(
            "Re-fetch the listed critical tickers (e.g. --tickers "
            f"{' '.join(crit_fail)} --provider prices=yfinance,stooq) or add a "
            "keyed price provider; do not treat their absence as an economic signal."
        )
    for label, key in (
        ("large-cap", "failed_large_cap_tickers"),
        ("user-watchlist", "failed_user_watchlist_tickers"),
    ):
        names = mat.get(key) or []
        if names:
            warnings.append(
                f"Material {label} ticker(s) missing from the ranking: "
                f"{', '.join(names)}."
            )

    # --- News is advisory: warn but never block. ---
    if not coverage.get("meets_news_coverage", True):
        nr = coverage.get("news", {}).get("coverage_ratio")
        warnings.append(
            f"News coverage {nr} is below "
            f"{rob.min_news_coverage_ratio}; sentiment confidence is reduced and "
            "sentiment should be treated as low-signal, not neutral-by-default."
        )

    # --- Coverage / failure warnings (also drive the degraded verdict). ---
    degraded = False
    if not coverage.get("meets_price_coverage", False):
        degraded = True
        pr = coverage.get("price", {}).get("coverage_ratio")
        reasons.append(
            f"Price coverage {pr} is below the required "
            f"{rob.min_price_coverage_ratio}."
        )
    if not coverage.get("meets_fundamentals_coverage", False):
        degraded = True
        fr = coverage.get("fundamentals", {}).get("coverage_ratio")
        reasons.append(
            f"Fundamentals coverage {fr} is below the required "
            f"{rob.min_fundamentals_coverage_ratio}."
        )
    if not coverage.get("within_provider_failure_budget", True):
        degraded = True
        pfr = coverage.get("provider_failure_ratio")
        reasons.append(
            f"Provider-side failure ratio {pfr} exceeds the budget "
            f"{rob.max_provider_failure_ratio} (looks like an outage, not just "
            "names without data)."
        )

    # 1) Systemic benchmark failure dominates everything.
    if blocking_systemic:
        bench = (health_report or {}).get("by_data_type", {})
        reasons.insert(
            0,
            "Benchmark health check reports a systemic provider failure "
            f"(price_systemic={bool((health_report or {}).get('price_systemic_failure'))}, "
            f"fundamentals_systemic={bool((health_report or {}).get('fundamentals_systemic_failure'))}). "
            "Mega-cap benchmarks (AAPL/MSFT/GOOGL) could not be fetched, so this "
            "is a provider outage, NOT a set of invalid tickers.",
        )
        recommendations.append(
            "Re-run later or switch providers (e.g. --provider prices=stooq,"
            "yfinance); a systemic outage cannot be fixed by re-ranking the "
            "survivors."
        )
        if rob.stop_on_systemic_provider_failure:
            validity = INVALID_PROVIDER_FAILURE
        else:
            validity = DIAGNOSTIC_ONLY
            warnings.append(
                "stop_on_systemic_provider_failure is off: emitting a "
                "diagnostic-only result despite a systemic failure."
            )
        return _verdict(validity, reasons, warnings, recommendations, bench=bench,
                        materiality=mat)

    # 2) Not enough real candidates to rank honestly.
    if not coverage.get("has_min_valid_candidates", False):
        reasons.insert(
            0,
            f"Only {coverage.get('valid_candidates', 0)} candidate(s) have real "
            f"fundamentals; the minimum to present a ranking is "
            f"{rob.min_valid_candidates_for_ranking}.",
        )
        recommendations.append(
            "Widen the universe or relax stage filters, and confirm the "
            "fundamentals provider is returning data."
        )
        return _verdict(INVALID_INSUFFICIENT_DATA, reasons, warnings, recommendations,
                        materiality=mat)

    # 3) Everything in budget -> trustworthy ranking.
    if not degraded:
        return _verdict(VALID_RANKING, reasons, warnings, recommendations,
                        materiality=mat)

    # 4) Degraded but rankable.
    if rob.allow_partial_ranking:
        recommendations.append(
            "Coverage is degraded; treat the ranking as partial. Re-run when the "
            "provider recovers, or add a fallback provider, before acting on it."
        )
        return _verdict(
            PARTIAL_RANKING_WITH_WARNINGS, reasons, warnings, recommendations,
            materiality=mat,
        )
    recommendations.append(
        "Coverage is degraded and partial ranking is disabled; emitting a "
        "diagnostic-only result. Re-run with --allow-partial-ranking to accept a "
        "caveated ranking, or improve coverage."
    )
    return _verdict(DIAGNOSTIC_ONLY, reasons, warnings, recommendations,
                    materiality=mat)


def _verdict(
    ranking_validity: str,
    reasons: List[str],
    warnings: List[str],
    recommendations: List[str],
    *,
    bench: Optional[Dict[str, Any]] = None,
    materiality: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    run_status = run_status_for(ranking_validity)
    mat = materiality or {}
    completeness = mat.get("ranking_completeness_status", COMPLETE)

    # Materiality is a second axis. A hard-critical (mega-cap/benchmark) miss on
    # an otherwise-VALID run downgrades the rollup to PARTIAL so the header and
    # return code reflect that the ranking is incomplete -- but it stays trusted
    # (exit 0) because the ordering of the names we DID price is still sound.
    if (
        completeness == PARTIAL_CRITICAL_TICKER_FAILURE
        and run_status == RUN_STATUS_VALID
    ):
        run_status = RUN_STATUS_PARTIAL

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_status": run_status,
        "ranking_validity": ranking_validity,
        "ranking_completeness_status": completeness,
        "is_trusted": is_trusted_run_status(run_status),
        "return_code": return_code_for(run_status),
        "invalid_ranking_reasons": list(reasons),
        "warnings": list(warnings),
        "recommendations_for_next_run": list(recommendations),
    }
    # Carry the material-gap detail so the summary/diagnostics can render it.
    for k in (
        "critical_ticker_failures",
        "hard_critical_ticker_failures",
        "failed_large_cap_tickers",
        "failed_high_liquidity_tickers",
        "failed_user_watchlist_tickers",
        "critical_failures_with_company_confirmed_real",
        "minor_non_material_price_gaps",
        "material_data_gaps",
    ):
        if k in mat:
            out[k] = mat[k]
    return out
