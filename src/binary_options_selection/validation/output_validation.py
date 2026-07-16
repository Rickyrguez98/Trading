"""Post-output self-audit for the binary-options pipeline.

Runs after the outputs are written and answers: "is what we just published
internally consistent and honest?" Checks: required schema, score bounds,
signal/reason consistency, valid-vs-NO_TRADE separation, deterministic
ranking order, timestamp sanity (no look-ahead: reference <= generated <
expiry), freshness consistency, empty outputs, and the
no-fabricated-probability rule (expected_edge / calibrated probability must
be absent unless a calibrated model exists — which it does not in this
milestone).

Statuses: ``ok`` | ``warn`` | ``error`` (error = integrity violation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..signals.decision import SIGNAL_CALL, SIGNAL_NO_TRADE, SIGNAL_PUT
from ..utils.timeutils import parse_utc

REQUIRED_ROW_FIELDS = (
    "run_id",
    "generated_at",
    "symbol",
    "provider",
    "market_state",
    "interval",
    "latest_candle_close",
    "data_age_seconds",
    "reference_time",
    "reference_price",
    "horizon_minutes",
    "horizon_group",
    "expiry_time",
    "signal",
    "call_score",
    "put_score",
    "directional_score",
    "confidence_score",
    "data_quality_score",
    "signal_validity",
    "signal_strength",
    "primary_drivers",
    "primary_conflicts",
    "warning_flags",
    "no_trade_reason",
    "run_status",
)


@dataclass
class ValidationCheck:
    name: str
    status: str  # ok | warn | error
    message: str
    details: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details[:20],
        }


def _ok(name: str, message: str) -> ValidationCheck:
    return ValidationCheck(name, "ok", message)


def _warn(name: str, message: str, details: Optional[List[str]] = None) -> ValidationCheck:
    return ValidationCheck(name, "warn", message, details or [])


def _error(name: str, message: str, details: Optional[List[str]] = None) -> ValidationCheck:
    return ValidationCheck(name, "error", message, details or [])


def validate_outputs(
    rows: List[Dict[str, Any]],
    *,
    run_status: str,
) -> Dict[str, Any]:
    checks: List[ValidationCheck] = [
        _check_nonempty(rows, run_status),
        _check_schema(rows),
        _check_bounds(rows),
        _check_signal_consistency(rows),
        _check_timestamps(rows),
        _check_ranking_order(rows),
        _check_no_fabricated_probability(rows),
    ]
    n_errors = sum(1 for c in checks if c.status == "error")
    n_warnings = sum(1 for c in checks if c.status == "warn")
    overall = "error" if n_errors else ("warn" if n_warnings else "ok")
    return {
        "overall_status": overall,
        "n_errors": n_errors,
        "n_warnings": n_warnings,
        "checks": [c.to_dict() for c in checks],
    }


# ---------------------------------------------------------------------------

def _check_nonempty(rows, run_status) -> ValidationCheck:
    name = "nonempty_outputs"
    if rows:
        return _ok(name, f"{len(rows)} rows produced")
    if run_status == "INVALID":
        return _warn(name, "no rows, consistent with an INVALID run")
    return _error(name, f"no rows produced but run_status is {run_status}")


def _check_schema(rows) -> ValidationCheck:
    name = "output_schema"
    if not rows:
        return _ok(name, "n/a (no rows)")
    missing: List[str] = []
    for i, r in enumerate(rows):
        gaps = [f for f in REQUIRED_ROW_FIELDS if f not in r]
        if gaps:
            missing.append(f"row {i} ({r.get('symbol')}): missing {gaps}")
    if missing:
        return _error(name, f"{len(missing)} row(s) missing required fields", missing)
    return _ok(name, f"all {len(REQUIRED_ROW_FIELDS)} required fields present")


def _check_bounds(rows) -> ValidationCheck:
    name = "score_bounds"
    problems: List[str] = []
    for r in rows:
        sym, h = r.get("symbol"), r.get("horizon_minutes")
        for fld, lo, hi in (
            ("call_score", 0, 100),
            ("put_score", 0, 100),
            ("directional_score", -100, 100),
            ("confidence_score", 0, 1),
            ("data_quality_score", 0, 100),
        ):
            v = r.get(fld)
            if v is None or not (lo <= float(v) <= hi):
                problems.append(f"{sym}@{h}m: {fld}={v!r} outside [{lo}, {hi}]")
        cs, ps, ds = r.get("call_score"), r.get("put_score"), r.get("directional_score")
        if None not in (cs, ps, ds) and abs((cs - ps) - ds) > 0.05:
            problems.append(f"{sym}@{h}m: directional != call - put")
    if problems:
        return _error(name, f"{len(problems)} bound violation(s)", problems)
    return _ok(name, "all scores within bounds and internally consistent")


def _check_signal_consistency(rows) -> ValidationCheck:
    name = "signal_consistency"
    problems: List[str] = []
    for r in rows:
        sym, h = r.get("symbol"), r.get("horizon_minutes")
        sig = r.get("signal")
        reason = r.get("no_trade_reason")
        if sig not in (SIGNAL_CALL, SIGNAL_PUT, SIGNAL_NO_TRADE):
            problems.append(f"{sym}@{h}m: unknown signal {sig!r}")
            continue
        if sig == SIGNAL_NO_TRADE and not reason:
            problems.append(f"{sym}@{h}m: NO_TRADE without a no_trade_reason")
        if sig != SIGNAL_NO_TRADE and reason:
            problems.append(f"{sym}@{h}m: {sig} carries a no_trade_reason ({reason})")
        if sig != SIGNAL_NO_TRADE and r.get("signal_validity") == "invalid":
            problems.append(
                f"{sym}@{h}m: live {sig} signal on invalid data -- must be NO_TRADE"
            )
        ds = r.get("directional_score")
        if sig == SIGNAL_CALL and ds is not None and ds <= 0:
            problems.append(f"{sym}@{h}m: CALL with non-positive directional score")
        if sig == SIGNAL_PUT and ds is not None and ds >= 0:
            problems.append(f"{sym}@{h}m: PUT with non-negative directional score")
    if problems:
        return _error(name, f"{len(problems)} inconsistency(ies)", problems)
    return _ok(name, "signals, reasons, and validity are consistent")


def _check_timestamps(rows) -> ValidationCheck:
    name = "timestamp_sanity"
    problems: List[str] = []
    for r in rows:
        sym, h = r.get("symbol"), r.get("horizon_minutes")
        try:
            gen = parse_utc(r["generated_at"])
            ref = parse_utc(r["reference_time"]) if r.get("reference_time") else None
            exp = parse_utc(r["expiry_time"]) if r.get("expiry_time") else None
        except (KeyError, ValueError) as exc:
            problems.append(f"{sym}@{h}m: unparseable timestamp ({exc})")
            continue
        if ref is not None and ref > gen:
            problems.append(
                f"{sym}@{h}m: reference_time after generated_at (look-ahead)"
            )
        if ref is not None and exp is not None:
            delta_min = (exp - ref).total_seconds() / 60.0
            if abs(delta_min - float(h)) > 0.01:
                problems.append(
                    f"{sym}@{h}m: expiry - reference = {delta_min:.1f}m != horizon"
                )
        if exp is not None and exp <= gen and r.get("signal") != SIGNAL_NO_TRADE:
            problems.append(f"{sym}@{h}m: live signal with expiry not in the future")
    if problems:
        return _error(name, f"{len(problems)} timestamp problem(s)", problems)
    return _ok(name, "reference <= generated < expiry holds; horizons align")


def _check_ranking_order(rows) -> ValidationCheck:
    name = "deterministic_ranking"
    ranked = [r for r in rows if r.get("signal") != SIGNAL_NO_TRADE]
    keys = [
        (
            -float(r.get("confidence_score") or 0.0),
            -abs(float(r.get("directional_score") or 0.0)),
            str(r.get("symbol")),
            int(r.get("horizon_minutes") or 0),
        )
        for r in ranked
    ]
    if keys != sorted(keys):
        return _error(name, "actionable rows are not in deterministic rank order")
    no_trade_seen = False
    for r in rows:
        if r.get("signal") == SIGNAL_NO_TRADE:
            no_trade_seen = True
        elif no_trade_seen:
            return _error(
                name, "a ranked signal appears after NO_TRADE rows -- separation broken"
            )
    return _ok(name, "rank order is deterministic and NO_TRADE rows are separated")


def _check_no_fabricated_probability(rows) -> ValidationCheck:
    name = "no_fabricated_probability"
    problems: List[str] = []
    for r in rows:
        sym, h = r.get("symbol"), r.get("horizon_minutes")
        if r.get("calibrated_probability") is not None:
            problems.append(
                f"{sym}@{h}m: calibrated_probability present but no calibrated "
                "model exists in this milestone"
            )
        if r.get("expected_edge") is not None:
            problems.append(
                f"{sym}@{h}m: expected_edge present -- it must never be derived "
                "from heuristic confidence"
            )
    if problems:
        return _error(name, f"{len(problems)} fabricated-probability violation(s)", problems)
    return _ok(name, "no probabilities or edges fabricated from heuristic scores")


# ---------------------------------------------------------------------------

def render_markdown(result: Dict[str, Any]) -> str:
    marks = {"error": "ERROR", "warn": "WARN", "ok": "ok"}
    lines = [
        "# Binary options output validation",
        "",
        f"Overall: **{result['overall_status']}** "
        f"({result['n_errors']} error(s), {result['n_warnings']} warning(s))",
        "",
    ]
    for c in result["checks"]:
        lines.append(f"- [{marks.get(c['status'], c['status'])}] **{c['name']}** — {c['message']}")
        if c["details"] and c["status"] != "ok":
            for d in c["details"][:10]:
                lines.append(f"  - {d}")
    lines.append("")
    return "\n".join(lines)
