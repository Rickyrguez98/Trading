"""Composite asset-selection score.

final_score =
    w_fundamentals * fundamentals_score
  + w_growth      * growth_score
  + w_quality     * quality_score
  + w_valuation   * valuation_score
  + w_sentiment   * sentiment_score
  - w_risk        * risk_penalty

All inputs are 0..100. Weights from the YAML config. The composite is
clipped to [0, 100] at the end.

risk_penalty (0..100, higher = more penalty) bundles:
    - low liquidity (avg dollar volume below threshold)
    - small market cap below threshold
    - high realized volatility (vs peers)
    - high missing-data count

We also produce two soft flags per row:
    - SPECULATIVE_HYPE                 (good sentiment, weak fundamentals)
    - STRONG_FUNDAMENTALS_BAD_SENTIMENT (worth a second look)
"""
from __future__ import annotations

import logging
import math
from typing import Dict, List

import numpy as np
import pandas as pd

from ..config import CompositeConfig, PricesConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk penalty
# ---------------------------------------------------------------------------

def compute_risk_penalty(
    df: pd.DataFrame,
    prices_cfg: PricesConfig,
    missing_penalty_weight: float = 1.5,
) -> pd.Series:
    """Return a 0..100 risk penalty per row.

    df must contain: avg_dollar_volume, market_cap, volatility_pct,
    missing_metric_count.
    """
    n = len(df)
    if n == 0:
        return pd.Series(dtype=float)

    penalty = pd.Series(0.0, index=df.index)

    # Liquidity penalty
    adv = pd.to_numeric(df.get("avg_dollar_volume"), errors="coerce")
    if adv is not None:
        below = adv.fillna(0) < prices_cfg.min_avg_dollar_volume
        penalty += below.astype(float) * 30.0
        # Soft penalty as a ratio for those above threshold but still thin
        ratio = (prices_cfg.min_avg_dollar_volume / adv.replace({0: np.nan})).clip(upper=1.0)
        penalty += ratio.fillna(0) * 10.0

    # Market-cap penalty
    mc = pd.to_numeric(df.get("market_cap"), errors="coerce")
    if mc is not None:
        below_mc = mc.fillna(0) < prices_cfg.min_market_cap
        penalty += below_mc.astype(float) * 25.0

    # Volatility penalty: penalize tickers whose vol is in the top quartile.
    vol = pd.to_numeric(df.get("volatility_pct"), errors="coerce")
    if vol is not None and vol.notna().any():
        q75 = vol.quantile(0.75)
        if math.isfinite(q75) and q75 > 0:
            excess = ((vol - q75) / q75).clip(lower=0.0).fillna(0.0)
            penalty += excess * 15.0

    # Missing data penalty
    miss = pd.to_numeric(df.get("missing_metric_count"), errors="coerce").fillna(0)
    penalty += miss * missing_penalty_weight

    return penalty.clip(lower=0.0, upper=100.0)


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_composite_scores(
    df: pd.DataFrame,
    composite_cfg: CompositeConfig,
) -> pd.Series:
    """Weighted blend of pillar scores minus risk penalty."""
    w = composite_cfg.weights or {}
    if not w:
        # Fallback equal weights so the pipeline still runs.
        w = {
            "fundamentals": 0.5,
            "growth": 0.15,
            "quality": 0.10,
            "valuation": 0.10,
            "sentiment": 0.15,
            "risk": 0.20,
        }

    fundamentals = pd.to_numeric(df.get("fundamentals_score", 50), errors="coerce").fillna(50)
    growth = pd.to_numeric(df.get("growth_score", 50), errors="coerce").fillna(50)
    quality = pd.to_numeric(df.get("quality_score", 50), errors="coerce").fillna(50)
    valuation = pd.to_numeric(df.get("valuation_score", 50), errors="coerce").fillna(50)
    sentiment = pd.to_numeric(df.get("sentiment_score", 50), errors="coerce").fillna(50)
    risk = pd.to_numeric(df.get("risk_penalty", 0), errors="coerce").fillna(0)

    composite = (
        w.get("fundamentals", 0.0) * fundamentals
        + w.get("growth", 0.0) * growth
        + w.get("quality", 0.0) * quality
        + w.get("valuation", 0.0) * valuation
        + w.get("sentiment", 0.0) * sentiment
        - w.get("risk", 0.0) * risk
    )
    return composite.clip(lower=0.0, upper=100.0)


# ---------------------------------------------------------------------------
# Flags + reasons
# ---------------------------------------------------------------------------

def flag_rows(
    df: pd.DataFrame,
    composite_cfg: CompositeConfig,
    low_sentiment_confidence_threshold: float = 0.3,
) -> pd.DataFrame:
    """Add ``flags`` (list[str]) and ``reason`` (str) columns to ``df``.

    ``low_sentiment_confidence_threshold`` is the confidence (0..1) below
    which we emit a ``LOW_SENTIMENT_CONFIDENCE`` flag, given there was at
    least one article (no news fires NO_NEWS instead).
    """
    if df.empty:
        df["flags"] = []
        df["reason"] = ""
        return df

    speculative_cfg = composite_cfg.speculative_hype or {}
    review_cfg = composite_cfg.strong_fundamentals_bad_sentiment or {}

    flags_per_row: List[List[str]] = []
    reasons: List[str] = []

    for _, row in df.iterrows():
        row_flags: List[str] = []

        f_score = float(row.get("fundamentals_score", 50.0))
        s_score = float(row.get("sentiment_score", 50.0))
        article_count = int(row.get("article_count", 0) or 0)
        missing_metric_count = int(row.get("missing_metric_count", 0) or 0)
        sentiment_confidence = float(row.get("sentiment_confidence", 0.0) or 0.0)

        if (
            s_score >= speculative_cfg.get("sentiment_min", 65)
            and f_score <= speculative_cfg.get("fundamentals_max", 40)
        ):
            row_flags.append("SPECULATIVE_HYPE")
        if (
            f_score >= review_cfg.get("fundamentals_min", 65)
            and s_score <= review_cfg.get("sentiment_max", 35)
        ):
            row_flags.append("STRONG_FUNDAMENTALS_BAD_SENTIMENT")
        if article_count == 0:
            row_flags.append("NO_NEWS")
        elif sentiment_confidence < low_sentiment_confidence_threshold:
            # Some news, but not enough volume/diversity to trust the signal.
            row_flags.append("LOW_SENTIMENT_CONFIDENCE")
        if missing_metric_count >= 5:
            row_flags.append("THIN_FUNDAMENTALS")
        if pd.isna(row.get("market_cap")):
            row_flags.append("MISSING_MARKET_CAP")

        flags_per_row.append(row_flags)
        reasons.append(_build_reason(row, row_flags))

    df = df.copy()
    df["flags"] = flags_per_row
    df["reason"] = reasons
    return df


def _build_reason(row: pd.Series, flags: List[str]) -> str:
    pieces = []
    f_score = float(row.get("fundamentals_score", 50.0))
    s_score = float(row.get("sentiment_score", 50.0))
    pieces.append(f"fundamentals={f_score:.1f}")
    pieces.append(f"sentiment={s_score:.1f}")
    if "growth_score" in row:
        pieces.append(f"growth={float(row['growth_score']):.1f}")
    if "valuation_score" in row:
        pieces.append(f"valuation={float(row['valuation_score']):.1f}")
    risk = float(row.get("risk_penalty", 0.0))
    if risk > 0:
        pieces.append(f"risk_penalty={risk:.1f}")
    if flags:
        pieces.append("flags=" + ",".join(flags))
    return " | ".join(pieces)
