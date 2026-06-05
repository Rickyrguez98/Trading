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
    - **weak price trend** (recent return below configured threshold and
      a cross-sectional momentum component)
    - high missing-data count

We also produce soft flags per row:
    - SPECULATIVE_HYPE                  (good sentiment, weak fundamentals)
    - STRONG_FUNDAMENTALS_BAD_SENTIMENT (worth a second look)
    - WEAK_PRICE_TREND                  (recent return below config threshold)
    - LOW_SENTIMENT_CONFIDENCE          (few articles / low source diversity)
    - NO_NEWS, THIN_FUNDAMENTALS, MISSING_MARKET_CAP (data-quality)
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
    if "avg_dollar_volume" in df.columns:
        adv = pd.to_numeric(df["avg_dollar_volume"], errors="coerce")
        below = adv.fillna(0) < prices_cfg.min_avg_dollar_volume
        penalty += below.astype(float) * 30.0
        # Soft penalty as a ratio for those above threshold but still thin
        ratio = (prices_cfg.min_avg_dollar_volume / adv.replace({0: np.nan})).clip(upper=1.0)
        penalty += ratio.fillna(0) * 10.0

    # Market-cap penalty
    if "market_cap" in df.columns:
        mc = pd.to_numeric(df["market_cap"], errors="coerce")
        below_mc = mc.fillna(0) < prices_cfg.min_market_cap
        penalty += below_mc.astype(float) * 25.0

    # Volatility penalty: penalize tickers whose vol is in the top quartile.
    if "volatility_pct" in df.columns:
        vol = pd.to_numeric(df["volatility_pct"], errors="coerce")
        if vol.notna().any():
            q75 = vol.quantile(0.75)
            if math.isfinite(q75) and q75 > 0:
                excess = ((vol - q75) / q75).clip(lower=0.0).fillna(0.0)
                penalty += excess * 15.0

    # Price-trend / momentum penalty: combine an absolute threshold (every
    # ticker whose return is below weak_return_threshold gets a fixed hit)
    # with a cross-sectional ramp (penalty grows as we approach the bottom
    # quintile of returns). The penalty stays bounded.
    if "return_pct" in df.columns:
        ret = pd.to_numeric(df["return_pct"], errors="coerce")
        if ret.notna().any():
            weak_thr = float(prices_cfg.weak_return_threshold)
            strength = max(0.0, float(prices_cfg.momentum_penalty_strength))
            below_threshold = (ret.fillna(0.0) < weak_thr)
            penalty += below_threshold.astype(float) * (0.5 * strength)

            # Cross-sectional: where do we sit in the universe?
            q20 = ret.quantile(0.20)
            if math.isfinite(q20):
                # Linear ramp: 0 at q20, max at the worst observed return.
                worst = ret.min()
                span = max(q20 - worst, 1e-6)
                ramp = ((q20 - ret) / span).clip(lower=0.0, upper=1.0).fillna(0.0)
                penalty += ramp * (0.5 * strength)

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
    weak_return_threshold: float = -0.10,
) -> pd.DataFrame:
    """Add ``flags`` (list[str]) and ``reason`` (str) columns to ``df``.

    ``low_sentiment_confidence_threshold`` is the confidence (0..1) below
    which we emit a ``LOW_SENTIMENT_CONFIDENCE`` flag, given there was at
    least one article (no news fires NO_NEWS instead).

    ``weak_return_threshold`` is the fractional return (e.g. -0.10 for -10%)
    below which we emit a ``WEAK_PRICE_TREND`` flag.
    """
    if df.empty:
        df["flags"] = []
        df["reason"] = ""
        return df

    speculative_cfg = composite_cfg.speculative_hype or {}
    review_cfg = composite_cfg.strong_fundamentals_bad_sentiment or {}

    flags_per_row: List[List[str]] = []
    reasons: List[str] = []
    top_driver_pillars: List[str] = []
    top_drag_pillars: List[str] = []

    for _, row in df.iterrows():
        row_flags: List[str] = []

        f_score = float(row.get("fundamentals_score", 50.0))
        s_score = float(row.get("sentiment_score", 50.0))
        article_count = int(row.get("article_count", 0) or 0)
        missing_metric_count = int(row.get("missing_metric_count", 0) or 0)
        sentiment_confidence = float(row.get("sentiment_confidence", 0.0) or 0.0)
        return_pct = row.get("return_pct")

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
        try:
            r = float(return_pct) if return_pct is not None else None
        except (TypeError, ValueError):
            r = None
        if r is not None and r == r and r < weak_return_threshold:
            row_flags.append("WEAK_PRICE_TREND")
        if missing_metric_count >= 5:
            row_flags.append("THIN_FUNDAMENTALS")
        if pd.isna(row.get("market_cap")):
            row_flags.append("MISSING_MARKET_CAP")

        flags_per_row.append(row_flags)
        reasons.append(_build_reason(row, row_flags))
        driver, drag = _pillar_drivers(row)
        top_driver_pillars.append(driver[0] if driver and driver[0] else "")
        top_drag_pillars.append(drag[0] if drag and drag[0] else "")

    df = df.copy()
    df["flags"] = flags_per_row
    df["reason"] = reasons
    df["top_driver_pillar"] = top_driver_pillars
    df["top_drag_pillar"] = top_drag_pillars
    return df


_PILLAR_COLUMNS = (
    ("growth", "growth_score"),
    ("quality", "quality_score"),
    ("valuation", "valuation_score"),
    ("balance_sheet", "balance_sheet_score"),
    ("cash_flow", "cash_flow_score"),
)


def _pillar_drivers(row: pd.Series) -> tuple:
    """Identify the pillar that most lifted the score and the one that most hurt.

    Returns ``(top_driver, top_drag)`` as ``(name, score)`` tuples or
    ``(None, None)`` if no pillar columns are present. We compare each
    pillar's deviation from the neutral midpoint of 50.
    """
    deltas = []
    for name, col in _PILLAR_COLUMNS:
        if col not in row.index:
            continue
        try:
            v = float(row[col])
        except (TypeError, ValueError):
            continue
        if v != v:
            continue
        deltas.append((name, v, v - 50.0))
    if not deltas:
        return (None, None)
    driver = max(deltas, key=lambda t: t[2])
    drag = min(deltas, key=lambda t: t[2])
    # Only call it a "driver" if it actually helped (positive delta);
    # likewise only a "drag" if it actually hurt.
    top_driver = (driver[0], driver[1]) if driver[2] > 0 else (None, None)
    top_drag = (drag[0], drag[1]) if drag[2] < 0 else (None, None)
    return (top_driver, top_drag)


def _build_reason(row: pd.Series, flags: List[str]) -> str:
    pieces = []
    f_score = float(row.get("fundamentals_score", 50.0))
    s_score = float(row.get("sentiment_score", 50.0))
    pieces.append(f"fundamentals={f_score:.1f}")
    pieces.append(f"sentiment={s_score:.1f}")

    driver, drag = _pillar_drivers(row)
    if driver[0]:
        pieces.append(f"top_driver={driver[0]}({driver[1]:.1f})")
    if drag[0]:
        pieces.append(f"top_drag={drag[0]}({drag[1]:.1f})")

    r = row.get("return_pct")
    try:
        r_val = float(r) if r is not None else None
    except (TypeError, ValueError):
        r_val = None
    if r_val is not None and r_val == r_val:
        pieces.append(f"return={r_val*100:+.1f}%")
    risk = float(row.get("risk_penalty", 0.0))
    if risk > 0:
        pieces.append(f"risk_penalty={risk:.1f}")
    if flags:
        pieces.append("flags=" + ",".join(flags))
    return " | ".join(pieces)
