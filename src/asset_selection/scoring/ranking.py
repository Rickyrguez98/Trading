"""Final ranking + human-readable report formatting."""
from __future__ import annotations

from typing import List

import pandas as pd


_DISPLAY_COLUMNS: List[str] = [
    "rank",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "market_cap",
    "avg_dollar_volume",
    "return_pct",
    "volatility_pct",
    "sentiment_score",
    "raw_sentiment_score",
    "effective_sentiment_score",
    "vader_sentiment_score",
    "finbert_sentiment_score",
    "sentiment_score_delta",
    "sentiment_model_agreement",
    "final_sentiment_score",
    "sentiment_model_used",
    "article_count",
    "unique_article_count",
    "duplicate_count",
    "stale_count",
    "fresh_ratio",
    "unique_ratio",
    "sentiment_confidence",
    "effective_sentiment_confidence",
    "positive_ratio",
    "negative_ratio",
    "source_diversity",
    "fundamentals_score",
    "growth_score",
    "quality_score",
    "valuation_score",
    "balance_sheet_score",
    "cash_flow_score",
    "top_driver_pillar",
    "top_drag_pillar",
    "strongest_metric",
    "strongest_metric_score",
    "weakest_metric",
    "weakest_metric_score",
    "market_cap_available",
    "valuation_metrics_available",
    "risk_penalty",
    "final_score",
    "selection_bucket",
    "flags",
    "reason",
    "missing_fields",
    "missing_metric_count",
]


def rank_candidates(df: pd.DataFrame, top_n: int = 25) -> pd.DataFrame:
    """Sort by ``final_score`` desc; tie-break with fundamentals then sentiment.

    Returns the full sorted DataFrame (not just the top N) so the CSV stays
    complete. The Markdown report formatter uses ``top_n`` separately.
    """
    if df.empty:
        return df
    sort_cols = [c for c in ("final_score", "fundamentals_score", "sentiment_score") if c in df.columns]
    ranked = df.sort_values(sort_cols, ascending=False).reset_index(drop=True)
    ranked.insert(0, "rank", ranked.index + 1)
    cols = [c for c in _DISPLAY_COLUMNS if c in ranked.columns]
    other_cols = [c for c in ranked.columns if c not in cols]
    return ranked[cols + other_cols]


# Per-section display columns (only those present are rendered).
_SHORTLIST_COLS = [
    "rank", "ticker", "company_name", "sector", "market_cap",
    "allocation_adjusted_score", "final_score", "fundamentals_score",
    "effective_sentiment_score", "return_pct", "volatility_pct",
    "risk_bucket", "candidate_role",
]
_RESEARCH_COLS = [
    "rank", "ticker", "company_name", "final_score", "fundamentals_score",
    "sentiment_score", "effective_sentiment_score",
    "vader_sentiment_score", "finbert_sentiment_score",
    "sentiment_model_agreement", "selection_bucket",
    "eligible_for_allocation", "allocation_adjusted_score",
    "return_pct", "volatility_pct", "flags",
]
_SPECULATIVE_COLS = [
    "rank", "ticker", "company_name", "final_score", "return_pct",
    "volatility_pct", "risk_bucket", "article_count", "flags",
]
_WATCHLIST_COLS = [
    "watchlist_rank", "ticker", "company_name", "final_score",
    "fundamentals_score", "return_pct", "data_quality_bucket",
    "exclusion_reason_from_allocation",
]
_EXCLUDED_COLS = [
    "rank", "ticker", "selection_bucket", "candidate_role",
    "recommended_next_step", "exclusion_reason_from_allocation",
]


def _prettify_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with human-friendly formatting for the report tables."""
    head = df.copy()
    for col in ("market_cap", "avg_dollar_volume"):
        if col in head.columns:
            head[col] = head[col].apply(_humanize_money)
    for col in (
        "final_score", "allocation_adjusted_score", "fundamentals_score",
        "sentiment_score", "raw_sentiment_score", "effective_sentiment_score",
        "vader_sentiment_score", "finbert_sentiment_score",
        "sentiment_score_delta", "final_sentiment_score",
        "growth_score", "quality_score", "valuation_score", "risk_penalty",
    ):
        if col in head.columns:
            head[col] = head[col].apply(_fmt_pct)
    for col in ("return_pct", "volatility_pct"):
        if col in head.columns:
            head[col] = head[col].apply(_fmt_signed_pct)
    for col in ("sentiment_confidence", "fresh_ratio", "effective_sentiment_confidence"):
        if col in head.columns:
            head[col] = head[col].apply(_fmt_confidence)
    if "eligible_for_allocation" in head.columns:
        head["eligible_for_allocation"] = head["eligible_for_allocation"].apply(
            lambda b: "yes" if bool(b) else "no"
        )
    for col in ("flags", "missing_fields", "allocation_exclusion_reasons"):
        if col in head.columns:
            head[col] = head[col].apply(
                lambda v: ", ".join(v) if isinstance(v, list) else (v or "")
            )
    return head


def _section_table(
    df: pd.DataFrame,
    cols,
    *,
    sort_by=None,
    ascending: bool = False,
    limit=None,
) -> str:
    """Sort + slice the raw frame, then render the requested columns."""
    sub = df
    if sort_by:
        keys = [sort_by] if isinstance(sort_by, str) else list(sort_by)
        keys = [c for c in keys if c in sub.columns]
        if keys:
            sub = sub.sort_values(keys, ascending=ascending)
    if limit is not None:
        sub = sub.head(limit)
    pretty = _prettify_for_display(sub)
    use_cols = [c for c in cols if c in pretty.columns]
    return pretty[use_cols].to_markdown(index=False)


def format_top_candidates_markdown(df: pd.DataFrame, top_n: int = 25) -> str:
    """Render the report, split by decision purpose.

    Five sections keep the *research ranking* visibly separate from the
    *allocation shortlist*:

      A. Portfolio-eligible shortlist (the table to use for allocation)
      B. Research ranking (everything, research-only)
      C. Speculative candidates (research-only)
      D. Watchlist-only candidates (manual review)
      E. Excluded-from-allocation reasons (why each top name was not eligible)
    """
    if df.empty:
        return "# Asset Selection — Research Ranking & Allocation Shortlist\n\n_No candidates produced._\n"

    has_alloc = "eligible_for_allocation" in df.columns
    eligible = df[df["eligible_for_allocation"].astype(bool)] if has_alloc else df.iloc[0:0]
    bucket = df.get("selection_bucket")
    spec = df[bucket == "speculative_candidate"] if bucket is not None else df.iloc[0:0]
    watch = df[bucket == "watchlist_only"] if bucket is not None else df.iloc[0:0]

    md = ["# Asset Selection — Research Ranking & Allocation Shortlist", ""]
    md.append("> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.")
    md.append("")
    md.append("This report answers two different questions, kept deliberately separate:")
    md.append("")
    md.append("- **Research ranking** — *which assets are worth considering?* Every scored "
              "candidate, sorted by `final_score`. Speculative and watchlist names are kept "
              "here, labeled, never hidden.")
    md.append("- **Allocation shortlist** — *which of those are safe to size by default?* "
              "Only candidates with `eligible_for_allocation = true`, sorted by "
              "`allocation_adjusted_score`. This is the subset a future allocation / "
              "rebalancing module would consume.")
    md.append("")
    md.append(
        f"_Ranked candidates: {len(df)} · allocation-eligible: {len(eligible)} · "
        f"speculative: {len(spec)} · watchlist-only: {len(watch)}._"
    )
    md.append("")

    # --- A. Portfolio-eligible shortlist ---
    md += ["## A. Portfolio-eligible shortlist", "",
           "Core/growth candidates that cleared the risk, data-quality, and sentiment "
           "gates, sorted by `allocation_adjusted_score`. **This is the table to use for "
           "allocation.**", ""]
    if eligible.empty:
        md.append("_No candidate is allocation-eligible in this run. Every top research name "
                  "was excluded — see section E for the reasons._")
    else:
        md.append(_section_table(
            eligible, _SHORTLIST_COLS,
            sort_by=["allocation_adjusted_score", "final_score"], limit=top_n,
        ))
    md.append("")

    # --- B. Research ranking ---
    md += ["## B. Research ranking (all candidates)", "",
           "The full ranking by `final_score`. Research-only: a high rank here is **not** an "
           "allocation recommendation. The `eligible_for_allocation` column shows what carried "
           "into the shortlist above.", ""]
    md.append(_section_table(df, _RESEARCH_COLS, sort_by=["final_score"], limit=top_n))
    md.append("")

    # --- C. Speculative candidates ---
    md += ["## C. Speculative candidates (research-only)", "",
           "High-volatility / speculative-momentum / hype names. Kept for research "
           "visibility; not allocation-eligible by default.", ""]
    md.append("_None._" if spec.empty else
              _section_table(spec, _SPECULATIVE_COLS, sort_by=["final_score"], limit=top_n))
    md.append("")

    # --- D. Watchlist-only candidates ---
    md += ["## D. Watchlist-only candidates (manual review)", "",
           "Weak trend, thin/poor fundamentals, missing market cap, or stale news. Need "
           "review before sizing; not allocation-eligible by default.", ""]
    md.append("_None._" if watch.empty else
              _section_table(watch, _WATCHLIST_COLS, sort_by=["final_score"], limit=top_n))
    md.append("")

    # --- E. Excluded-from-allocation reasons ---
    md += ["## E. Excluded-from-allocation reasons (top-ranked names)", "",
           "Why each top-ranked research name did not make the allocation shortlist.", ""]
    top_research = df.sort_values("final_score", ascending=False).head(top_n)
    ineligible = (
        top_research[~top_research["eligible_for_allocation"].astype(bool)]
        if has_alloc else top_research.iloc[0:0]
    )
    md.append("_Every top-ranked name is allocation-eligible._" if ineligible.empty else
              _section_table(ineligible, _EXCLUDED_COLS, sort_by=["final_score"], limit=top_n))
    md.append("")

    md += _legend_section()
    return "\n".join(md) + "\n"


def _legend_section() -> List[str]:
    md: List[str] = []
    md.append("## Flag legend")
    md.append("")
    md.append("- **SPECULATIVE_HYPE** — strong sentiment but weak fundamentals.")
    md.append("- **STRONG_FUNDAMENTALS_BAD_SENTIMENT** — quality business, negative recent news.")
    md.append("- **NO_NEWS** — no recent articles available; sentiment score is neutral by default.")
    md.append("- **LOW_SENTIMENT_CONFIDENCE** — few articles or low source diversity; treat sentiment as noisy.")
    md.append("- **STALE_NEWS** — most recent coverage is aging (low fresh_ratio); sentiment weight is damped.")
    md.append("- **VERY_STALE_NEWS** — nearly all coverage is stale; sentiment is pulled toward neutral.")
    md.append("- **LOW_SOURCE_DIVERSITY** — sentiment rests on too few distinct sources; treat as a single voice.")
    md.append("- **WEAK_PRICE_TREND** — recent return is in the bottom of the cross-section; treat with caution.")
    md.append("- **THIN_FUNDAMENTALS** — many missing fundamental fields; score is less reliable.")
    md.append("- **MISSING_MARKET_CAP** — could not read market cap; size/liquidity filters degraded.")
    md.append("- **HIGH_VOLATILITY** — annualized volatility above the configured ceiling; size positions accordingly.")
    md.append("- **SPECULATIVE_MOMENTUM** — large run-up on a very noisy tape; reward may be chasing risk.")
    md.append("")
    md.append("## Selection buckets (research) vs. candidate roles (allocation)")
    md.append("")
    md.append("- **high_quality_core_candidate** → role `core_candidate` — strong fundamentals, contained volatility, low risk penalty, non-negative trend.")
    md.append("- **growth_candidate** → role `satellite_growth_candidate` — decent fundamentals with elevated (but not extreme) volatility; eligible only if it clears the allocation gates.")
    md.append("- **speculative_candidate** → role `speculative_research_only` — high volatility, speculative momentum/hype, or high risk penalty. Labeled, not removed; not eligible by default.")
    md.append("- **watchlist_only** → role `watchlist_only` — weak trend, thin/poor fundamentals, or missing market cap; needs review before sizing; not eligible by default.")
    md.append("")
    md.append("## How to read `allocation_adjusted_score`")
    md.append("")
    md.append("`allocation_adjusted_score` starts from `final_score` and subtracts penalties for "
              "high volatility, speculative momentum, weak trend, watchlist/speculative bucket, "
              "low sentiment confidence, stale news, and excess risk penalty. It is the sort key "
              "for the allocation shortlist, so a high research score with high risk lands lower "
              "than a steadier name.")
    return md


def _humanize_money(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x != x:  # NaN
        return ""
    abs_x = abs(x)
    if abs_x >= 1e12:
        return f"${x/1e12:.2f}T"
    if abs_x >= 1e9:
        return f"${x/1e9:.2f}B"
    if abs_x >= 1e6:
        return f"${x/1e6:.2f}M"
    if abs_x >= 1e3:
        return f"${x/1e3:.2f}K"
    return f"${x:.0f}"


def _fmt_pct(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x != x:
        return ""
    return f"{x:.1f}"


def _fmt_signed_pct(v) -> str:
    """Format a fractional return (0.12) as a signed percentage (+12.0%)."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x != x:
        return ""
    return f"{x*100:+.1f}%"


def _fmt_confidence(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    if x != x:
        return ""
    return f"{x:.2f}"
