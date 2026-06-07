"""Separate a *research ranking* from an *allocation shortlist*.

The composite score answers a research question: "which assets are worth
considering?" It deliberately keeps speculative and watchlist names in the
ranking (labeled, never hidden). But "ranked highly" must not be confused with
"safe to size". This module adds the fields a future allocation/rebalancing
module needs to make that distinction, **without** removing anything from the
research ranking and **without** implementing allocation itself.

For every ranked candidate it derives:

* ``eligible_for_allocation``      -- bool; default true only for core/growth
                                      that clear the risk/data/sentiment gates.
* ``allocation_adjusted_score``    -- ``final_score`` minus risk/quality
                                      penalties; the sort key for the shortlist.
* ``allocation_exclusion_reasons`` -- machine-readable reason codes (list).
* ``exclusion_reason_from_allocation`` -- a one-line human summary ("" if eligible).
* ``risk_bucket``                  -- low_risk | moderate_risk | high_risk.
* ``sentiment_quality_bucket``     -- no_news | low/moderate/high_confidence.
* ``data_quality_bucket``          -- complete | partial | thin | incomplete.
* ``candidate_role``               -- core_candidate | satellite_growth_candidate
                                      | speculative_research_only | watchlist_only.
* ``recommended_next_step``        -- what a human/optimizer should do next.
* ``watchlist_rank``               -- rank among watchlist_only names (else None).

The module is pure (DataFrame in, DataFrame out) so it is trivially testable.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..config import AllocationConfig, RiskControlsConfig, SentimentConfig

# selection_bucket -> candidate_role
_ROLE_BY_BUCKET = {
    "high_quality_core_candidate": "core_candidate",
    "growth_candidate": "satellite_growth_candidate",
    "speculative_candidate": "speculative_research_only",
    "watchlist_only": "watchlist_only",
}

# Machine reason code -> human-readable phrase for the one-line summary.
_REASON_GLOSS = {
    "speculative_candidate": "speculative bucket (volatile / momentum / hype)",
    "watchlist_only": "watchlist-only bucket (weak trend / thin data)",
    "high_volatility": "volatility above the allocation ceiling",
    "excessive_risk_penalty": "risk penalty above the allocation budget",
    "weak_price_trend": "weak recent price trend",
    "negative_recent_return": "negative recent return",
    "missing_data": "too many missing fundamental fields",
    "missing_market_cap": "missing market cap",
    "low_sentiment_confidence": "low sentiment confidence",
    "stale_news": "sentiment leans on stale news",
}

_RISK_REASONS = frozenset(
    {"high_volatility", "excessive_risk_penalty", "weak_price_trend",
     "negative_recent_return"}
)
_SENTIMENT_REASONS = frozenset({"low_sentiment_confidence", "stale_news"})


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


def _flags(row: pd.Series) -> List[str]:
    v = row.get("flags")
    if isinstance(v, list):
        return list(v)
    if isinstance(v, str) and v:
        return [s.strip() for s in v.split(",") if s.strip()]
    return []


# ---------------------------------------------------------------------------
# Per-row derivations
# ---------------------------------------------------------------------------

def _eligibility(
    row: pd.Series, flags: List[str], cfg: AllocationConfig
) -> Tuple[bool, List[str]]:
    """Return ``(eligible, reason_codes)`` for one candidate.

    Core/growth buckets are eligible *unless* a gate adds a reason. Speculative
    and watchlist buckets always carry a bucket reason (unless explicitly
    allowed), so they are excluded by default but still scored and reported.
    """
    reasons: List[str] = []
    bucket = str(row.get("selection_bucket") or "")
    vol = _num(row.get("volatility_pct"))
    risk_penalty = _num(row.get("risk_penalty")) or 0.0
    r = _num(row.get("return_pct"))
    conf = _num(row.get("sentiment_confidence")) or 0.0
    fresh = _num(row.get("fresh_ratio"))
    article_count = int(row.get("article_count") or 0)
    missing = int(row.get("missing_metric_count") or 0)
    market_cap = _num(row.get("market_cap"))

    # 1. Bucket gating -- speculative/watchlist are not portfolio-ready by default.
    if bucket == "speculative_candidate" and not cfg.allow_speculative_for_allocation:
        reasons.append("speculative_candidate")
    if bucket == "watchlist_only" and not cfg.allow_watchlist_for_allocation:
        reasons.append("watchlist_only")

    # 2. Risk gating.
    if vol is not None and vol > cfg.max_allocation_volatility:
        reasons.append("high_volatility")
    if risk_penalty > cfg.max_allocation_risk_penalty:
        reasons.append("excessive_risk_penalty")
    if "WEAK_PRICE_TREND" in flags:
        reasons.append("weak_price_trend")
    if (
        cfg.require_non_negative_recent_return_for_allocation
        and r is not None and r < 0.0
    ):
        reasons.append("negative_recent_return")

    # 3. Data-quality gating.
    if "THIN_FUNDAMENTALS" in flags or missing > cfg.max_missing_metric_count_for_allocation:
        reasons.append("missing_data")
    if cfg.require_market_cap_for_allocation and (
        market_cap is None or "MISSING_MARKET_CAP" in flags
    ):
        reasons.append("missing_market_cap")

    # 4. Sentiment gating -- only when the name actually has news. A name with no
    #    recent coverage is NOT blocked (sentiment is advisory and its effective
    #    contribution is neutral); a name whose thin/stale feed could mislead is.
    if article_count > 0:
        if conf < cfg.min_sentiment_confidence_for_allocation:
            reasons.append("low_sentiment_confidence")
        if fresh is not None and fresh < cfg.min_fresh_news_ratio_for_allocation:
            reasons.append("stale_news")

    # De-dup while preserving order.
    seen: set = set()
    deduped = [x for x in reasons if not (x in seen or seen.add(x))]
    return (len(deduped) == 0, deduped)


def _allocation_adjusted_score(
    row: pd.Series, flags: List[str], cfg: AllocationConfig
) -> float:
    """``final_score`` minus penalties so risk/quality concerns demote a name in
    the shortlist even when its raw research score is high."""
    base = _num(row.get("final_score"))
    if base is None:
        return 0.0
    bucket = str(row.get("selection_bucket") or "")
    risk_penalty = _num(row.get("risk_penalty")) or 0.0
    fresh = _num(row.get("fresh_ratio"))
    article_count = int(row.get("article_count") or 0)

    adj = base
    if "HIGH_VOLATILITY" in flags:
        adj -= cfg.penalty_high_volatility
    if "SPECULATIVE_MOMENTUM" in flags:
        adj -= cfg.penalty_speculative_momentum
    if "WEAK_PRICE_TREND" in flags:
        adj -= cfg.penalty_weak_price_trend
    if bucket == "watchlist_only":
        adj -= cfg.penalty_watchlist_bucket
    if bucket == "speculative_candidate":
        adj -= cfg.penalty_speculative_bucket
    if "LOW_SENTIMENT_CONFIDENCE" in flags:
        adj -= cfg.penalty_low_sentiment_confidence
    stale = "STALE_NEWS" in flags or "VERY_STALE_NEWS" in flags or (
        article_count > 0 and fresh is not None
        and fresh < cfg.min_fresh_news_ratio_for_allocation
    )
    if stale:
        adj -= cfg.penalty_stale_news
    if risk_penalty > cfg.max_allocation_risk_penalty:
        adj -= cfg.penalty_excess_risk_weight * (
            risk_penalty - cfg.max_allocation_risk_penalty
        )
    return float(max(0.0, min(100.0, adj)))


def _risk_bucket(row: pd.Series, rc: RiskControlsConfig) -> str:
    vol = _num(row.get("volatility_pct"))
    risk_penalty = _num(row.get("risk_penalty")) or 0.0
    if (vol is None or vol <= rc.core_max_volatility_pct) and risk_penalty <= rc.core_max_risk_penalty:
        return "low_risk"
    if (vol is None or vol <= rc.max_volatility_pct) and risk_penalty <= rc.max_risk_penalty:
        return "moderate_risk"
    return "high_risk"


def _sentiment_quality_bucket(row: pd.Series, scfg: SentimentConfig) -> str:
    article_count = int(row.get("article_count") or 0)
    if article_count == 0:
        return "no_news"
    conf = _num(row.get("sentiment_confidence")) or 0.0
    if conf >= 0.60:
        return "high_confidence"
    if conf >= scfg.low_confidence_threshold:
        return "moderate_confidence"
    return "low_confidence"


def _data_quality_bucket(row: pd.Series, flags: List[str]) -> str:
    market_cap = _num(row.get("market_cap"))
    missing = int(row.get("missing_metric_count") or 0)
    if market_cap is None or "MISSING_MARKET_CAP" in flags:
        return "incomplete"
    if "THIN_FUNDAMENTALS" in flags or missing >= 5:
        return "thin"
    if missing == 0:
        return "complete"
    return "partial"


def _recommended_next_step(
    eligible: bool, bucket: str, reasons: List[str]
) -> str:
    if eligible:
        return "eligible_for_portfolio_optimizer"
    if bucket in ("speculative_candidate", "watchlist_only"):
        return "exclude_from_allocation_by_default"
    if any(x in _RISK_REASONS for x in reasons):
        return "risk_review_needed"
    if any(x in _SENTIMENT_REASONS for x in reasons):
        return "sentiment_refresh_needed"
    return "needs_manual_review"


def _human_exclusion(reasons: List[str]) -> str:
    if not reasons:
        return ""
    return "; ".join(_REASON_GLOSS.get(r, r) for r in reasons)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_allocation_fields(
    df: pd.DataFrame,
    *,
    allocation_cfg: AllocationConfig,
    risk_controls: RiskControlsConfig,
    sentiment_cfg: SentimentConfig,
) -> pd.DataFrame:
    """Add the allocation-eligibility columns to a ranked DataFrame.

    Operates on the *whole* ranked frame so ``watchlist_rank`` is global. Returns
    a copy; never drops or reorders rows.
    """
    df = df.copy()
    if df.empty:
        for col in (
            "eligible_for_allocation", "allocation_adjusted_score",
            "allocation_exclusion_reasons", "exclusion_reason_from_allocation",
            "risk_bucket", "sentiment_quality_bucket", "data_quality_bucket",
            "candidate_role", "recommended_next_step", "watchlist_rank",
        ):
            df[col] = [] if col.endswith("reasons") else None
        return df

    eligible_col: List[bool] = []
    adj_col: List[float] = []
    reasons_col: List[List[str]] = []
    human_col: List[str] = []
    risk_col: List[str] = []
    sent_col: List[str] = []
    data_col: List[str] = []
    role_col: List[str] = []
    next_col: List[str] = []

    for _, row in df.iterrows():
        flags = _flags(row)
        bucket = str(row.get("selection_bucket") or "")
        eligible, reasons = _eligibility(row, flags, allocation_cfg)
        eligible_col.append(eligible)
        adj_col.append(_allocation_adjusted_score(row, flags, allocation_cfg))
        reasons_col.append(reasons)
        human_col.append(_human_exclusion(reasons))
        risk_col.append(_risk_bucket(row, risk_controls))
        sent_col.append(_sentiment_quality_bucket(row, sentiment_cfg))
        data_col.append(_data_quality_bucket(row, flags))
        role_col.append(_ROLE_BY_BUCKET.get(bucket, "watchlist_only"))
        next_col.append(_recommended_next_step(eligible, bucket, reasons))

    df["eligible_for_allocation"] = eligible_col
    df["allocation_adjusted_score"] = adj_col
    df["allocation_exclusion_reasons"] = reasons_col
    df["exclusion_reason_from_allocation"] = human_col
    df["risk_bucket"] = risk_col
    df["sentiment_quality_bucket"] = sent_col
    df["data_quality_bucket"] = data_col
    df["candidate_role"] = role_col
    df["recommended_next_step"] = next_col

    # watchlist_rank: 1 = best watchlist_only name by final_score; None otherwise.
    is_watch = df["selection_bucket"] == "watchlist_only"
    df["watchlist_rank"] = pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    if is_watch.any():
        sort_key = "final_score" if "final_score" in df.columns else None
        watch_idx = (
            df.loc[is_watch].sort_values(sort_key, ascending=False).index
            if sort_key else df.loc[is_watch].index
        )
        for i, idx in enumerate(watch_idx, start=1):
            df.at[idx, "watchlist_rank"] = i
    return df


def allocation_field_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a candidate row's allocation fields into a JSON-safe dict.

    Used by the summary builder so the same field set appears in CSV, JSON, and
    Markdown.
    """
    wr = row.get("watchlist_rank")
    try:
        wr = int(wr) if wr is not None and not (isinstance(wr, float) and math.isnan(wr)) else None
    except (TypeError, ValueError):
        wr = None
    adj = row.get("allocation_adjusted_score")
    try:
        adj = float(adj) if adj is not None else None
    except (TypeError, ValueError):
        adj = None
    return {
        "eligible_for_allocation": bool(row.get("eligible_for_allocation")),
        "allocation_adjusted_score": adj,
        "allocation_exclusion_reasons": list(row.get("allocation_exclusion_reasons") or []),
        "exclusion_reason_from_allocation": row.get("exclusion_reason_from_allocation") or "",
        "risk_bucket": row.get("risk_bucket") or None,
        "sentiment_quality_bucket": row.get("sentiment_quality_bucket") or None,
        "data_quality_bucket": row.get("data_quality_bucket") or None,
        "candidate_role": row.get("candidate_role") or None,
        "recommended_next_step": row.get("recommended_next_step") or None,
        "watchlist_rank": wr,
    }
