"""Run-status determination: VALID / PARTIAL / DEGRADED / INVALID.

* ``VALID``    — every requested symbol processed cleanly; benchmark context
                 present; no provider failures.
* ``PARTIAL``  — some symbols failed to fetch or were screened out, but the
                 rows that were produced are clean. Requires
                 ``robustness.allow_partial_results``.
* ``DEGRADED`` — rows were produced but the data behind a material share of
                 them is compromised (invalid-validity rows, missing benchmark
                 context, unknown market state), or partial results are not
                 allowed. A degraded run is never presented like a valid one:
                 the status appears in the banner, the JSON, and every row.
* ``INVALID``  — the run cannot be trusted at all: universe resolution failed,
                 a systemic provider outage, provider-side failure ratio at or
                 above ``robustness.max_provider_failure_ratio``, or zero rows.

Precedence: INVALID > DEGRADED > PARTIAL > VALID.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RUN_VALID = "VALID"
RUN_PARTIAL = "PARTIAL"
RUN_DEGRADED = "DEGRADED"
RUN_INVALID = "INVALID"

# Share of produced rows with signal_validity == "invalid" at/above which the
# run is DEGRADED rather than merely PARTIAL.
_INVALID_ROW_RATIO_FOR_DEGRADED = 0.25


@dataclass
class RunAssessment:
    run_status: str = RUN_INVALID
    reasons: List[str] = field(default_factory=list)
    symbols_requested: int = 0
    symbols_fetched: int = 0
    symbols_failed: int = 0
    provider_side_failures: int = 0
    rows_produced: int = 0
    invalid_row_count: int = 0
    benchmark_available: bool = False
    market_state: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_status": self.run_status,
            "reasons": self.reasons,
            "symbols_requested": self.symbols_requested,
            "symbols_fetched": self.symbols_fetched,
            "symbols_failed": self.symbols_failed,
            "provider_side_failures": self.provider_side_failures,
            "rows_produced": self.rows_produced,
            "invalid_row_count": self.invalid_row_count,
            "benchmark_available": self.benchmark_available,
            "market_state": self.market_state,
        }


def determine_run_status(
    *,
    universe_error: Optional[str],
    symbols_requested: int,
    symbols_fetched: int,
    provider_side_failures: int,
    systemic_health_failure: bool,
    rows: List[Dict[str, Any]],
    benchmark_available: bool,
    market_state: Optional[str],
    allow_partial_results: bool,
    max_provider_failure_ratio: float,
) -> RunAssessment:
    a = RunAssessment(
        symbols_requested=symbols_requested,
        symbols_fetched=symbols_fetched,
        symbols_failed=max(0, symbols_requested - symbols_fetched),
        provider_side_failures=provider_side_failures,
        rows_produced=len(rows),
        invalid_row_count=sum(1 for r in rows if r.get("signal_validity") == "invalid"),
        benchmark_available=benchmark_available,
        market_state=market_state,
    )

    # --- INVALID ---
    if universe_error:
        a.run_status = RUN_INVALID
        a.reasons.append(f"universe resolution failed: {universe_error}")
        return a
    if systemic_health_failure:
        a.run_status = RUN_INVALID
        a.reasons.append("benchmark health probe reports a systemic provider outage")
        return a
    failure_ratio = (
        provider_side_failures / symbols_requested if symbols_requested else 0.0
    )
    if symbols_requested and failure_ratio >= max_provider_failure_ratio:
        a.run_status = RUN_INVALID
        a.reasons.append(
            f"provider-side failures on {failure_ratio:.0%} of symbols "
            f"(threshold {max_provider_failure_ratio:.0%})"
        )
        return a
    if not rows:
        a.run_status = RUN_INVALID
        a.reasons.append("no rows produced")
        return a

    # --- DEGRADED ---
    invalid_ratio = a.invalid_row_count / len(rows)
    degraded_reasons: List[str] = []
    if not benchmark_available:
        degraded_reasons.append("benchmark context unavailable for every row")
    if invalid_ratio >= _INVALID_ROW_RATIO_FOR_DEGRADED:
        degraded_reasons.append(
            f"{invalid_ratio:.0%} of rows carry invalid data validity"
        )
    if market_state == "unknown":
        degraded_reasons.append("market state could not be determined")
    if a.symbols_failed > 0 and not allow_partial_results:
        degraded_reasons.append(
            f"{a.symbols_failed} symbol(s) failed and partial results are disabled"
        )
    if degraded_reasons:
        a.run_status = RUN_DEGRADED
        a.reasons.extend(degraded_reasons)
        return a

    # --- PARTIAL ---
    if a.symbols_failed > 0 or a.invalid_row_count > 0:
        a.run_status = RUN_PARTIAL
        if a.symbols_failed:
            a.reasons.append(f"{a.symbols_failed} of {symbols_requested} symbols not processed")
        if a.invalid_row_count:
            a.reasons.append(f"{a.invalid_row_count} row(s) carry invalid data validity")
        return a

    a.run_status = RUN_VALID
    return a


def render_status_banner(assessment: RunAssessment, generated_at: str) -> str:
    """Markdown banner placed above every report table."""
    status = assessment.run_status
    icons = {RUN_VALID: "OK", RUN_PARTIAL: "PARTIAL", RUN_DEGRADED: "DEGRADED", RUN_INVALID: "INVALID"}
    lines = [
        f"> **Run status: {icons.get(status, status)}** — generated {generated_at}.",
    ]
    if status != RUN_VALID:
        for r in assessment.reasons:
            lines.append(f"> - {r}")
        lines.append(
            "> Interpret the table below with these caveats; a "
            f"{status.lower()} run is not equivalent to a valid one."
        )
    lines.append(
        "> Research signals only — not financial advice, not an execution "
        "system, and confidence scores are **not** probabilities."
    )
    return "\n".join(lines)
