"""Post-run output validation -> reports/output_validation.{json,md}.

Each validator returns a :class:`ValidationCheck`. A check is *informational*
unless it finds a problem, in which case it is raised to ``warn`` and records
the offending rows so a human can audit them. Nothing here mutates or drops
candidates -- this module only *reports*. It deliberately re-derives findings
from the produced output rather than trusting the pipeline's own counters.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import AppConfig
from ..universe import _NAME_BLOCKLIST

# Pillar score columns we inspect for single-pillar dominance.
_PILLAR_COLS = (
    "growth_score",
    "quality_score",
    "valuation_score",
    "balance_sheet_score",
    "cash_flow_score",
)


@dataclass
class ValidationCheck:
    """One named check. ``severity`` is the worst outcome it can report."""

    name: str
    status: str               # "ok" | "warn"
    message: str
    count: int = 0
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "count": self.count,
            "examples": self.examples,
        }


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return x


def _flags(row: pd.Series) -> List[str]:
    v = row.get("flags")
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


def _ok(name: str, message: str) -> ValidationCheck:
    return ValidationCheck(name=name, status="ok", message=message)


def _warn(name: str, message: str, examples: List[Dict[str, Any]]) -> ValidationCheck:
    return ValidationCheck(
        name=name, status="warn", message=message,
        count=len(examples), examples=examples[:25],
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_excluded_security_types(ranked: pd.DataFrame) -> ValidationCheck:
    """A ranked candidate should never be an ETF/warrant/unit/preferred/etc."""
    name = "excluded_security_types_in_results"
    if ranked.empty or "company_name" not in ranked.columns:
        return _ok(name, "No company names to scan.")
    leaks: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        cname = str(row.get("company_name") or "")
        if not cname:
            continue
        for kind, pattern in _NAME_BLOCKLIST.items():
            if pattern.search(cname):
                leaks.append({
                    "ticker": row.get("ticker"),
                    "company_name": cname,
                    "matched_type": kind,
                })
                break
    if leaks:
        return _warn(
            name,
            f"{len(leaks)} ranked candidate(s) look like excluded security types "
            "(ETF/warrant/unit/preferred/rights/notes/when-issued). They should "
            "have been removed during universe cleaning.",
            leaks,
        )
    return _ok(name, "No excluded security types found among ranked candidates.")


def _check_provider_failures(summary: Dict[str, Any]) -> ValidationCheck:
    """Surface the honest provider-failure block, not just 'it finished'."""
    name = "provider_failures"
    pf = (summary or {}).get("provider_failures") or {}
    total = int(pf.get("total", 0) or 0)
    if total <= 0:
        return _ok(name, "No provider errors or empty responses recorded.")
    by_reason = pf.get("by_reason") or {}
    examples = pf.get("examples") or []
    return _warn(
        name,
        f"{total} provider failure(s) recorded across stages "
        f"(by reason: {by_reason}). Inspect whether data is genuinely "
        "unavailable or the provider rate-limited/blocked the request.",
        list(examples),
    )


def _check_stale_news(ranked: pd.DataFrame, cfg: AppConfig) -> ValidationCheck:
    name = "stale_news"
    if ranked.empty:
        return _ok(name, "No candidates to check.")
    offenders: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        stale = _num(row.get("stale_count"))
        fresh = _num(row.get("fresh_ratio"))
        n = _num(row.get("article_count"))
        if n and ((stale and stale > 0) or (fresh is not None and fresh < 1.0)):
            offenders.append({
                "ticker": row.get("ticker"),
                "article_count": int(n),
                "stale_count": int(stale or 0),
                "fresh_ratio": round(fresh, 2) if fresh is not None else None,
            })
    if offenders:
        return _warn(
            name,
            f"{len(offenders)} candidate(s) carry stale news (older than "
            f"{cfg.sentiment.stale_after_days:g} days). Their sentiment leans on "
            "aging coverage; treat the signal as weaker.",
            offenders,
        )
    return _ok(name, "No stale news detected among ranked candidates.")


def _check_extreme_volatility(ranked: pd.DataFrame, cfg: AppConfig) -> ValidationCheck:
    name = "extreme_volatility"
    if ranked.empty:
        return _ok(name, "No candidates to check.")
    ceiling = cfg.risk_controls.max_volatility_pct
    offenders: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        vol = _num(row.get("volatility_pct"))
        flags = _flags(row)
        if (vol is not None and vol > ceiling) or "HIGH_VOLATILITY" in flags:
            offenders.append({
                "ticker": row.get("ticker"),
                "volatility_pct": round(vol, 3) if vol is not None else None,
                "selection_bucket": row.get("selection_bucket"),
                "flags": flags,
            })
    if offenders:
        return _warn(
            name,
            f"{len(offenders)} candidate(s) exceed the volatility ceiling "
            f"({ceiling:.0%} annualized). They are labeled (HIGH_VOLATILITY / "
            "speculative bucket), not removed -- size positions accordingly.",
            offenders,
        )
    return _ok(name, "No candidate exceeds the volatility ceiling.")


def _check_missing_market_cap(ranked: pd.DataFrame) -> ValidationCheck:
    name = "missing_market_cap"
    if ranked.empty:
        return _ok(name, "No candidates to check.")
    offenders: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        mc = _num(row.get("market_cap"))
        avail = row.get("market_cap_available")
        flags = _flags(row)
        missing = (
            "MISSING_MARKET_CAP" in flags
            or mc is None
            or (avail is not None and not bool(avail))
        )
        if missing:
            offenders.append({
                "ticker": row.get("ticker"),
                "market_cap": mc,
                "market_cap_available": bool(avail) if avail is not None else None,
            })
    if offenders:
        return _warn(
            name,
            f"{len(offenders)} candidate(s) are missing market cap; size and "
            "liquidity filters were degraded for them.",
            offenders,
        )
    return _ok(name, "All ranked candidates have a market cap.")


def _check_overestimated_confidence(ranked: pd.DataFrame, cfg: AppConfig) -> ValidationCheck:
    """High confidence should require many UNIQUE, DIVERSE articles."""
    name = "overestimated_sentiment_confidence"
    if ranked.empty:
        return _ok(name, "No candidates to check.")
    full_n = cfg.sentiment.confidence_full_article_count
    offenders: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        conf = _num(row.get("sentiment_confidence"))
        if conf is None or conf < 0.8:
            continue
        unique = _num(row.get("unique_article_count"))
        diversity = _num(row.get("source_diversity"))
        thin = (unique is not None and unique < full_n / 2) or (
            diversity is not None and diversity < 2
        )
        if thin:
            offenders.append({
                "ticker": row.get("ticker"),
                "sentiment_confidence": round(conf, 3),
                "unique_article_count": int(unique) if unique is not None else None,
                "source_diversity": int(diversity) if diversity is not None else None,
            })
    if offenders:
        return _warn(
            name,
            f"{len(offenders)} candidate(s) report confidence >= 0.80 on a thin "
            "or single-source feed. Confidence may be overstated -- verify the "
            "underlying article volume and diversity.",
            offenders,
        )
    return _ok(name, "No sentiment confidence looks overstated.")


def _check_sentiment_models(
    ranked: pd.DataFrame, summary: Dict[str, Any]
) -> ValidationCheck:
    """Report the VADER/FinBERT comparison status honestly.

    Fires a warning (something to weigh, never a row drop) when FinBERT was
    requested but is unavailable in comparison mode, or when the two models
    disagree on some tickers. When no comparison was configured it is a benign
    'ok' naming the single model that ran.
    """
    name = "sentiment_model_comparison"
    s = (summary or {}).get("sentiment_summary") or {}
    if not s:
        return _ok(name, "No sentiment-model summary recorded.")

    used = s.get("sentiment_model_used", "vader")
    comparison = bool(s.get("comparison_mode"))
    finbert_avail = bool(s.get("finbert_available"))
    disagree = [t for t in (s.get("tickers_with_large_disagreement") or []) if t]
    examples: List[Dict[str, Any]] = []

    if comparison and not finbert_avail:
        examples.append({
            "ticker": "(run)",
            "issue": "FINBERT_UNAVAILABLE",
            "detail": s.get("finbert_unavailable_reason")
            or "FinBERT requested in comparison mode but not usable; "
               "scored with VADER only.",
        })
    for t in disagree:
        examples.append({
            "ticker": t,
            "issue": "SENTIMENT_MODEL_DISAGREEMENT",
            "detail": "VADER and FinBERT differ beyond the configured threshold.",
        })

    if examples:
        return _warn(
            name,
            f"Sentiment ran as '{used}' (comparison={comparison}, "
            f"finbert_available={finbert_avail}). "
            f"{s.get('sentiment_model_disagreement_count', 0)} large "
            "VADER/FinBERT disagreement(s). Treat flagged tickers' sentiment as "
            "model-dependent; fundamentals still dominate the composite.",
            examples,
        )
    return _ok(
        name,
        f"Sentiment ran as '{used}' "
        f"(comparison={comparison}, finbert_available={finbert_avail}); "
        "no large model disagreements.",
    )


def _check_single_pillar_dominance(ranked: pd.DataFrame) -> ValidationCheck:
    """Flag candidates whose fundamentals_score rests on one pillar alone."""
    name = "single_pillar_dominance"
    have = [c for c in _PILLAR_COLS if c in ranked.columns]
    if ranked.empty or len(have) < 2:
        return _ok(name, "Not enough pillar columns to assess dominance.")
    offenders: List[Dict[str, Any]] = []
    for _, row in ranked.iterrows():
        scores = {c: _num(row.get(c)) for c in have}
        present = {c: v for c, v in scores.items() if v is not None}
        if len(present) < 2:
            continue
        top_col = max(present, key=present.get)
        top = present[top_col]
        rest = [v for c, v in present.items() if c != top_col]
        rest_mean = sum(rest) / len(rest)
        # One pillar carries the score: it is strong while the others are weak.
        if top >= 60.0 and (top - rest_mean) >= 20.0 and all(v < 50.0 for v in rest):
            offenders.append({
                "ticker": row.get("ticker"),
                "dominant_pillar": top_col.replace("_score", ""),
                "dominant_score": round(top, 1),
                "other_pillar_mean": round(rest_mean, 1),
            })
    if offenders:
        return _warn(
            name,
            f"{len(offenders)} candidate(s) have a fundamentals score carried by a "
            "single pillar while the others are weak. The headline number is less "
            "robust than it looks.",
            offenders,
        )
    return _ok(name, "No single-pillar-dominated fundamentals scores found.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate_outputs(
    ranked: pd.DataFrame,
    summary: Dict[str, Any],
    config: AppConfig,
) -> Dict[str, Any]:
    """Run every check and return a structured report dict."""
    checks: List[ValidationCheck] = [
        _check_excluded_security_types(ranked),
        _check_provider_failures(summary or {}),
        _check_stale_news(ranked, config),
        _check_extreme_volatility(ranked, config),
        _check_missing_market_cap(ranked),
        _check_overestimated_confidence(ranked, config),
        _check_sentiment_models(ranked, summary or {}),
        _check_single_pillar_dominance(ranked),
    ]
    warnings = [c for c in checks if c.status == "warn"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_candidates": int(len(ranked)),
        "n_warnings": len(warnings),
        "overall_status": "warn" if warnings else "ok",
        "checks": [c.to_dict() for c in checks],
    }


def write_validation_reports(report: Dict[str, Any], output_dir: Path) -> "tuple[Path, Path]":
    """Write output_validation.json and output_validation.md; return their paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "output_validation.json"
    md_path = output_dir / "output_validation.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _render_markdown(report: Dict[str, Any]) -> str:
    status = report.get("overall_status", "ok").upper()
    lines = [
        "# Output Validation",
        "",
        f"_Generated: {report.get('generated_at')}_",
        "",
        "> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.",
        "",
        f"**Overall status: {status}** "
        f"({report.get('n_warnings', 0)} warning(s) over "
        f"{report.get('n_candidates', 0)} ranked candidate(s)).",
        "",
        "These checks re-audit the produced output the way a skeptical reviewer "
        "would. A warning does not mean a candidate was removed -- nothing here "
        "drops rows; it flags quality concerns for a human to weigh.",
        "",
        "| Check | Status | Count | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for c in report.get("checks", []):
        mark = "WARN" if c.get("status") == "warn" else "ok"
        msg = str(c.get("message", "")).replace("\n", " ")
        lines.append(f"| {c.get('name')} | {mark} | {c.get('count', 0)} | {msg} |")
    lines.append("")

    # Detail blocks for any check that fired.
    for c in report.get("checks", []):
        if c.get("status") != "warn" or not c.get("examples"):
            continue
        lines.append(f"## {c.get('name')}")
        lines.append("")
        examples = c.get("examples", [])
        try:
            lines.append(pd.DataFrame(examples).to_markdown(index=False))
        except Exception:  # noqa: BLE001 - tabulate missing, fall back to JSON
            for ex in examples:
                lines.append(f"- {json.dumps(ex, default=str)}")
        lines.append("")
    return "\n".join(lines) + "\n"
