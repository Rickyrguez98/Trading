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
    "article_count",
    "unique_article_count",
    "duplicate_count",
    "stale_count",
    "fresh_ratio",
    "unique_ratio",
    "sentiment_confidence",
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


def format_top_candidates_markdown(df: pd.DataFrame, top_n: int = 25) -> str:
    """Render the top-N rows as a self-contained Markdown report."""
    if df.empty:
        return "# Asset Selection — Top Candidates\n\n_No candidates produced._\n"

    head = df.head(top_n).copy()
    # Pretty-print the things people will actually read.
    if "market_cap" in head.columns:
        head["market_cap"] = head["market_cap"].apply(_humanize_money)
    if "avg_dollar_volume" in head.columns:
        head["avg_dollar_volume"] = head["avg_dollar_volume"].apply(_humanize_money)
    for col in (
        "final_score",
        "fundamentals_score",
        "sentiment_score",
        "growth_score",
        "quality_score",
        "valuation_score",
        "risk_penalty",
    ):
        if col in head.columns:
            head[col] = head[col].apply(_fmt_pct)
    if "return_pct" in head.columns:
        head["return_pct"] = head["return_pct"].apply(_fmt_signed_pct)
    if "volatility_pct" in head.columns:
        head["volatility_pct"] = head["volatility_pct"].apply(_fmt_signed_pct)
    if "sentiment_confidence" in head.columns:
        head["sentiment_confidence"] = head["sentiment_confidence"].apply(_fmt_confidence)

    if "flags" in head.columns:
        head["flags"] = head["flags"].apply(lambda v: ", ".join(v) if isinstance(v, list) else (v or ""))
    if "missing_fields" in head.columns:
        head["missing_fields"] = head["missing_fields"].apply(
            lambda v: ", ".join(v) if isinstance(v, list) else (v or "")
        )

    columns_in_order = [
        "rank",
        "ticker",
        "company_name",
        "sector",
        "market_cap",
        "final_score",
        "fundamentals_score",
        "sentiment_score",
        "growth_score",
        "valuation_score",
        "return_pct",
        "volatility_pct",
        "risk_penalty",
        "selection_bucket",
        "article_count",
        "sentiment_confidence",
        "top_driver_pillar",
        "top_drag_pillar",
        "flags",
    ]
    cols = [c for c in columns_in_order if c in head.columns]

    md = ["# Asset Selection — Top Candidates", ""]
    md.append(f"_Top {min(top_n, len(head))} of {len(df)} ranked candidates._")
    md.append("")
    md.append("> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.")
    md.append("")
    md.append(head[cols].to_markdown(index=False))
    md.append("")
    md.append("## Flag legend")
    md.append("")
    md.append("- **SPECULATIVE_HYPE** — strong sentiment but weak fundamentals.")
    md.append("- **STRONG_FUNDAMENTALS_BAD_SENTIMENT** — quality business, negative recent news.")
    md.append("- **NO_NEWS** — no recent articles available; sentiment score is neutral by default.")
    md.append("- **LOW_SENTIMENT_CONFIDENCE** — few articles or low source diversity; treat sentiment as noisy.")
    md.append("- **WEAK_PRICE_TREND** — recent return is in the bottom of the cross-section; treat with caution.")
    md.append("- **THIN_FUNDAMENTALS** — many missing fundamental fields; score is less reliable.")
    md.append("- **MISSING_MARKET_CAP** — could not read market cap; size/liquidity filters degraded.")
    md.append("- **HIGH_VOLATILITY** — annualized volatility above the configured ceiling; size positions accordingly.")
    md.append("- **SPECULATIVE_MOMENTUM** — large run-up on a very noisy tape; reward may be chasing risk.")
    md.append("")
    md.append("## Selection buckets")
    md.append("")
    md.append("- **high_quality_core_candidate** — strong fundamentals, contained volatility, low risk penalty, non-negative trend.")
    md.append("- **growth_candidate** — decent fundamentals with elevated (but not extreme) volatility.")
    md.append("- **speculative_candidate** — high volatility, speculative momentum/hype, or high risk penalty. Labeled, not removed.")
    md.append("- **watchlist_only** — weak trend, thin/poor fundamentals, or missing market cap; needs more review before sizing.")
    return "\n".join(md) + "\n"


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
