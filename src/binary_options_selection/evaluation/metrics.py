"""Aggregation of replay records into honest performance metrics.

Return arithmetic per decided record with a known outcome (payout ratio r =
net profit per unit staked): win -> +r, loss -> -1, tie -> 0 (stake returned;
platforms differ — the tie rule is reported, not hidden). ``UNKNOWN`` outcomes
are excluded from return math and reported separately: an unresolvable
outcome is not a loss.

No calibration metrics are produced: the confidence score is a heuristic,
not a probability. Instead, ``performance_by_confidence_bucket`` shows
whether higher-confidence buckets *historically* did better.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..signals.decision import SIGNAL_CALL, SIGNAL_NO_TRADE, SIGNAL_PUT
from ..signals.labels import TIE, UNKNOWN
from .engine import EvalRecord, EvaluationConfig


def _rate(part: int, whole: int) -> Optional[float]:
    return round(part / whole, 4) if whole else None


def summarize(records: List[EvalRecord], eval_cfg: EvaluationConfig) -> Dict[str, Any]:
    n = len(records)
    calls = [r for r in records if r.signal == SIGNAL_CALL]
    puts = [r for r in records if r.signal == SIGNAL_PUT]
    no_trades = [r for r in records if r.signal == SIGNAL_NO_TRADE]
    decided = calls + puts
    resolved = [r for r in decided if r.outcome != UNKNOWN]
    unknown = [r for r in decided if r.outcome == UNKNOWN]
    ties = [r for r in resolved if r.outcome == TIE]

    call_resolved = [r for r in calls if r.outcome not in (UNKNOWN, TIE)]
    put_resolved = [r for r in puts if r.outcome not in (UNKNOWN, TIE)]
    directional = call_resolved + put_resolved
    wins = [r for r in directional if r.correct]
    losses = [r for r in directional if r.correct is False]

    returns: List[float] = []
    for r in resolved:
        if r.outcome == TIE:
            returns.append(0.0)
        elif r.correct:
            returns.append(r.payout_ratio)
        else:
            returns.append(-1.0)
    avg_payout = (
        round(sum(r.payout_ratio for r in decided) / len(decided), 4) if decided else None
    )

    summary: Dict[str, Any] = {
        "signal_count": n,
        "call_count": len(calls),
        "put_count": len(puts),
        "no_trade_count": len(no_trades),
        "no_trade_rate": _rate(len(no_trades), n),
        "coverage_rate": _rate(len(decided), n),
        "call_win_rate": _rate(sum(1 for r in call_resolved if r.correct), len(call_resolved)),
        "put_win_rate": _rate(sum(1 for r in put_resolved if r.correct), len(put_resolved)),
        "combined_win_rate": _rate(len(wins), len(directional)),
        "tie_rate": _rate(len(ties), len(resolved)),
        "unknown_rate": _rate(len(unknown), len(decided)),
        "average_payout": avg_payout,
        "break_even_win_rate": (
            round(1.0 / (1.0 + avg_payout), 4) if avg_payout else None
        ),
        "realized_return_per_unit_staked": (
            round(sum(returns) / len(returns), 4) if returns else None
        ),
        "maximum_consecutive_losses": _max_consecutive_losses(directional),
        "maximum_drawdown": _max_drawdown(returns),
        "wins": len(wins),
        "losses": len(losses),
    }
    return summary


def _max_consecutive_losses(directional: List[EvalRecord]) -> int:
    worst = cur = 0
    for r in sorted(directional, key=lambda x: (x.timestamp, x.horizon_minutes)):
        if r.correct is False:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def _max_drawdown(returns: Sequence[float]) -> Optional[float]:
    if not returns:
        return None
    peak = cum = 0.0
    max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return round(max_dd, 4)


def _bucket_label(confidence: float, edges: Sequence[float]) -> str:
    lo = 0.0
    for edge in edges:
        if confidence < edge:
            return f"[{lo:.2f},{edge:.2f})"
        lo = edge
    return f"[{lo:.2f},1.00]"


def full_report(
    records: List[EvalRecord],
    eval_cfg: EvaluationConfig,
) -> Dict[str, Any]:
    """Overall + per-split + per-slice metrics for one record set."""
    report: Dict[str, Any] = {"overall": summarize(records, eval_cfg)}
    for split in ("train", "validation", "test"):
        report[split] = summarize([r for r in records if r.split == split], eval_cfg)
    report["performance_by_horizon"] = {
        f"{h}m": summarize([r for r in records if r.horizon_minutes == h], eval_cfg)
        for h in sorted({r.horizon_minutes for r in records})
    }
    report["performance_by_horizon_group"] = {
        g: summarize([r for r in records if r.horizon_group == g], eval_cfg)
        for g in sorted({r.horizon_group for r in records})
    }
    report["performance_by_confidence_bucket"] = {
        b: summarize(rs, eval_cfg)
        for b, rs in _group_by(
            records, lambda r: _bucket_label(r.confidence, eval_cfg.confidence_bucket_edges)
        ).items()
    }
    report["performance_by_signal_strength"] = {
        b: summarize(rs, eval_cfg)
        for b, rs in _group_by(records, lambda r: r.strength).items()
    }
    report["performance_by_asset_type"] = {
        eval_cfg.asset_type: report["overall"]
    }
    report["performance_by_session"] = {
        eval_cfg.session_label: report["overall"]
    }
    report["notes"] = [
        "confidence_score is a bounded heuristic, not a calibrated probability; "
        "no calibration metrics are computed",
        "UNKNOWN outcomes are excluded from return arithmetic and reported "
        "separately -- an unresolvable outcome is not a loss",
        "historical performance does not guarantee future results",
    ]
    return report


def compare_to_baselines(
    reports: Dict[str, Dict[str, Any]],
    *,
    split: str = "test",
    min_observations: int = 30,
) -> Dict[str, Any]:
    """Out-of-sample comparison. The strategy may only be described as adding
    value when it beats the relevant baselines on the untouched split with
    enough observations — otherwise the verdict is honest: insufficient or
    not outperforming."""
    strategy = reports.get("strategy", {}).get(split, {})
    s_ret = strategy.get("realized_return_per_unit_staked")
    s_n = (strategy.get("wins") or 0) + (strategy.get("losses") or 0)
    rows = {}
    beats_all = True
    for name, rep in reports.items():
        if name == "strategy":
            continue
        b = rep.get(split, {})
        rows[name] = {
            "realized_return_per_unit_staked": b.get("realized_return_per_unit_staked"),
            "combined_win_rate": b.get("combined_win_rate"),
        }
        b_ret = b.get("realized_return_per_unit_staked")
        if s_ret is None or (b_ret is not None and s_ret <= b_ret):
            beats_all = False
    if s_n < min_observations:
        verdict = (
            f"insufficient out-of-sample observations ({s_n} < {min_observations}); "
            "no predictive-power claim can be made"
        )
    elif beats_all:
        verdict = (
            "strategy outperformed every naive baseline on the untouched "
            f"{split} split ({s_n} resolved signals); this is still historical "
            "evidence, not a guarantee"
        )
    else:
        verdict = (
            "strategy did NOT outperform all naive baselines out of sample; "
            "no predictive power should be claimed"
        )
    return {
        "split": split,
        "strategy": {
            "realized_return_per_unit_staked": s_ret,
            "combined_win_rate": strategy.get("combined_win_rate"),
            "resolved_signals": s_n,
        },
        "baselines": rows,
        "verdict": verdict,
    }


def _group_by(records: List[EvalRecord], key) -> Dict[str, List[EvalRecord]]:
    out: Dict[str, List[EvalRecord]] = {}
    for r in records:
        out.setdefault(key(r), []).append(r)
    return dict(sorted(out.items()))
