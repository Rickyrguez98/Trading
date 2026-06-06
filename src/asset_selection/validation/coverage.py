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
# Ranking-validity verdict
# ---------------------------------------------------------------------------

def determine_run_status(
    coverage: Dict[str, Any],
    health_report: Optional[Dict[str, Any]],
    config: AppConfig,
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
        return _verdict(validity, reasons, warnings, recommendations, bench=bench)

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
        return _verdict(INVALID_INSUFFICIENT_DATA, reasons, warnings, recommendations)

    # 3) Everything in budget -> trustworthy ranking.
    if not degraded:
        return _verdict(VALID_RANKING, reasons, warnings, recommendations)

    # 4) Degraded but rankable.
    if rob.allow_partial_ranking:
        recommendations.append(
            "Coverage is degraded; treat the ranking as partial. Re-run when the "
            "provider recovers, or add a fallback provider, before acting on it."
        )
        return _verdict(
            PARTIAL_RANKING_WITH_WARNINGS, reasons, warnings, recommendations
        )
    recommendations.append(
        "Coverage is degraded and partial ranking is disabled; emitting a "
        "diagnostic-only result. Re-run with --allow-partial-ranking to accept a "
        "caveated ranking, or improve coverage."
    )
    return _verdict(DIAGNOSTIC_ONLY, reasons, warnings, recommendations)


def _verdict(
    ranking_validity: str,
    reasons: List[str],
    warnings: List[str],
    recommendations: List[str],
    *,
    bench: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    run_status = run_status_for(ranking_validity)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_status": run_status,
        "ranking_validity": ranking_validity,
        "is_trusted": is_trusted_run_status(run_status),
        "return_code": return_code_for(run_status),
        "invalid_ranking_reasons": list(reasons),
        "warnings": list(warnings),
        "recommendations_for_next_run": list(recommendations),
    }
