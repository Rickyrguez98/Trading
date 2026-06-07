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

from ..config import (
    CompositeConfig,
    PricesConfig,
    RiskControlsConfig,
    SentimentConfig,
)

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
# Effective (confidence-adjusted) sentiment
# ---------------------------------------------------------------------------

def compute_effective_confidence(
    df: pd.DataFrame,
    sentiment_cfg: SentimentConfig,
) -> pd.Series:
    """Damp the reported confidence when news is stale.

    Below ``stale_news_fresh_ratio_threshold`` the confidence is scaled down in
    proportion to how fresh the feed is (``fresh_ratio / threshold``, capped at
    1.0), so sentiment that leans on aging coverage carries less weight in the
    effective-sentiment formula. With ``stale_news_penalty_enabled`` False this
    returns the raw confidence unchanged.
    """
    conf = pd.to_numeric(df.get("sentiment_confidence", 0.0), errors="coerce").fillna(0.0).clip(lower=0.0, upper=1.0)
    if not sentiment_cfg.stale_news_penalty_enabled:
        return conf
    thr = float(sentiment_cfg.stale_news_fresh_ratio_threshold)
    if thr <= 0:
        return conf
    fresh = pd.to_numeric(df.get("fresh_ratio", 1.0), errors="coerce").fillna(1.0).clip(lower=0.0, upper=1.0)
    damp = (fresh / thr).clip(upper=1.0)
    return (conf * damp).clip(lower=0.0, upper=1.0)


def compute_effective_sentiment(
    df: pd.DataFrame,
    sentiment_cfg: SentimentConfig,
) -> pd.Series:
    """Pull raw sentiment toward neutral in proportion to (1 - confidence).

    ``effective = neutral + confidence * (raw - neutral)``

    A high-confidence 80/100 sentiment stays near 80; a low-confidence 80/100 is
    pulled back toward ``neutral_sentiment_score`` so a thin, noisy, or stale feed
    cannot swing the composite. Confidence is taken from
    ``effective_sentiment_confidence`` when present (stale-news damping, see
    flag_rows) and otherwise from the raw ``sentiment_confidence``.

    When ``use_confidence_adjusted_sentiment`` is False this is a pass-through of
    the raw sentiment, so the behaviour is fully config-reversible.
    """
    neutral = float(sentiment_cfg.neutral_sentiment_score)
    raw = pd.to_numeric(df.get("sentiment_score", neutral), errors="coerce").fillna(neutral)
    if not sentiment_cfg.use_confidence_adjusted_sentiment:
        return raw.clip(lower=0.0, upper=100.0)
    if "effective_sentiment_confidence" in df.columns:
        conf_src = df["effective_sentiment_confidence"]
    else:
        conf_src = df.get("sentiment_confidence", 0.0)
    conf = pd.to_numeric(conf_src, errors="coerce").fillna(0.0).clip(lower=0.0, upper=1.0)
    effective = neutral + conf * (raw - neutral)
    return effective.clip(lower=0.0, upper=100.0)


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def compute_composite_scores(
    df: pd.DataFrame,
    composite_cfg: CompositeConfig,
    sentiment_column: str = "sentiment_score",
) -> pd.Series:
    """Weighted blend of pillar scores minus risk penalty.

    ``sentiment_column`` selects which sentiment series feeds the blend. The
    pipeline passes ``effective_sentiment_score`` (confidence-adjusted) by
    default; pass ``sentiment_score`` to use the raw value. Falls back to
    ``sentiment_score`` if the requested column is absent.
    """
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
    sent_col = sentiment_column if sentiment_column in df.columns else "sentiment_score"
    sentiment = pd.to_numeric(df.get(sent_col, 50), errors="coerce").fillna(50)
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
    risk_controls: "RiskControlsConfig | None" = None,
    stale_news_fresh_ratio_threshold: float = 0.50,
    very_stale_news_fresh_ratio_threshold: float = 0.20,
    low_source_diversity_threshold: int = 2,
) -> pd.DataFrame:
    """Add ``flags`` (list[str]), ``reason``, and ``selection_bucket`` columns.

    ``low_sentiment_confidence_threshold`` is the confidence (0..1) below
    which we emit a ``LOW_SENTIMENT_CONFIDENCE`` flag, given there was at
    least one article (no news fires NO_NEWS instead).

    ``weak_return_threshold`` is the fractional return (e.g. -0.10 for -10%)
    below which we emit a ``WEAK_PRICE_TREND`` flag.

    ``risk_controls`` drives the new ``HIGH_VOLATILITY`` / ``SPECULATIVE_MOMENTUM``
    flags and the ``selection_bucket`` label. Volatile names are labeled, never
    dropped here.
    """
    rc = risk_controls or RiskControlsConfig()

    if df.empty:
        df["flags"] = []
        df["reason"] = ""
        df["selection_bucket"] = ""
        return df

    speculative_cfg = composite_cfg.speculative_hype or {}
    review_cfg = composite_cfg.strong_fundamentals_bad_sentiment or {}

    flags_per_row: List[List[str]] = []
    reasons: List[str] = []
    top_driver_pillars: List[str] = []
    top_drag_pillars: List[str] = []
    buckets: List[str] = []

    for _, row in df.iterrows():
        row_flags: List[str] = []

        f_score = float(row.get("fundamentals_score", 50.0))
        s_score = float(row.get("sentiment_score", 50.0))
        article_count = int(row.get("article_count", 0) or 0)
        missing_metric_count = int(row.get("missing_metric_count", 0) or 0)
        sentiment_confidence = float(row.get("sentiment_confidence", 0.0) or 0.0)
        return_pct = row.get("return_pct")
        risk_penalty = float(row.get("risk_penalty", 0.0) or 0.0)
        vol = _coerce(row.get("volatility_pct"))
        r = _coerce(return_pct)

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
        fresh_ratio = _coerce(row.get("fresh_ratio"))
        source_diversity = int(row.get("source_diversity", 0) or 0)
        if article_count == 0:
            row_flags.append("NO_NEWS")
        else:
            if sentiment_confidence < low_sentiment_confidence_threshold:
                # Some news, but not enough volume/diversity to trust the signal.
                row_flags.append("LOW_SENTIMENT_CONFIDENCE")
            # Freshness: VERY_STALE_NEWS supersedes STALE_NEWS.
            if (
                fresh_ratio is not None
                and fresh_ratio <= very_stale_news_fresh_ratio_threshold
            ):
                row_flags.append("VERY_STALE_NEWS")
            elif (
                fresh_ratio is not None
                and fresh_ratio < stale_news_fresh_ratio_threshold
            ):
                row_flags.append("STALE_NEWS")
            if source_diversity < low_source_diversity_threshold:
                row_flags.append("LOW_SOURCE_DIVERSITY")
        if r is not None and r < weak_return_threshold:
            row_flags.append("WEAK_PRICE_TREND")
        if missing_metric_count >= 5:
            row_flags.append("THIN_FUNDAMENTALS")
        if pd.isna(row.get("market_cap")):
            row_flags.append("MISSING_MARKET_CAP")
        # New risk-control flags.
        if vol is not None and vol > rc.max_volatility_pct:
            row_flags.append("HIGH_VOLATILITY")
        if (
            r is not None and vol is not None
            and r >= rc.speculative_return_pct
            and vol >= rc.speculative_volatility_pct
        ):
            row_flags.append("SPECULATIVE_MOMENTUM")

        flags_per_row.append(row_flags)
        buckets.append(_selection_bucket(f_score, vol, r, risk_penalty, row_flags, rc))
        reasons.append(_build_reason(row, row_flags))
        driver, drag = _pillar_drivers(row)
        top_driver_pillars.append(driver[0] if driver and driver[0] else "")
        top_drag_pillars.append(drag[0] if drag and drag[0] else "")

    df = df.copy()
    df["flags"] = flags_per_row
    df["selection_bucket"] = buckets
    df["reason"] = reasons
    df["top_driver_pillar"] = top_driver_pillars
    df["top_drag_pillar"] = top_drag_pillars
    return df


def _coerce(v) -> "float | None":
    try:
        x = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    if x is None or x != x:  # None or NaN
        return None
    return x


def _selection_bucket(
    f_score: float,
    vol: "float | None",
    r: "float | None",
    risk_penalty: float,
    flags: List[str],
    rc: RiskControlsConfig,
) -> str:
    """Bucket a candidate so volatile/speculative names are visibly separated
    from steady core names. Evaluated by priority: speculative first (riskiest),
    then watchlist (quality/data concern), then core, then growth.
    """
    speculative = (
        "HIGH_VOLATILITY" in flags
        or "SPECULATIVE_MOMENTUM" in flags
        or "SPECULATIVE_HYPE" in flags
        or risk_penalty > rc.max_risk_penalty
        or (vol is not None and vol > rc.max_volatility_pct)
    )
    if speculative:
        return "speculative_candidate"

    if (
        "WEAK_PRICE_TREND" in flags
        or "THIN_FUNDAMENTALS" in flags
        or "MISSING_MARKET_CAP" in flags
        or f_score < rc.watchlist_max_fundamentals
    ):
        return "watchlist_only"

    if (
        f_score >= rc.core_min_fundamentals
        and (vol is None or vol <= rc.core_max_volatility_pct)
        and risk_penalty <= rc.core_max_risk_penalty
        and (r is None or r >= 0.0)
    ):
        return "high_quality_core_candidate"

    return "growth_candidate"


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
        # Keep a consistent shape so callers can always do driver[0]/drag[0].
        return ((None, None), (None, None))
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
