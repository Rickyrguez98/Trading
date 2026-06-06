"""Cross-sectional fundamental scoring.

Pipeline:
    raw metric DataFrame
      -> winsorize at config percentiles
      -> z-score across universe
      -> map to [0, 100] via shifted/scaled sigmoid-ish transform
      -> weighted average per pillar (renormalized over non-missing weights)
      -> missing-data penalty per pillar
      -> aggregate into fundamentals_score with config pillar weights
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..config import ScoringConfig
from ..data_providers.base import Fundamentals
from .growth_metrics import GROWTH_METRICS, extract_growth_metrics
from .quality_metrics import QUALITY_METRICS, extract_quality_metrics
from .valuation_metrics import VALUATION_METRICS, extract_valuation_metrics

logger = logging.getLogger(__name__)


# Metrics where lower is better. The scorer inverts the z-score for these.
_INVERTED_METRICS = set(VALUATION_METRICS) | {"debt_to_equity"}

# Balance-sheet metrics. debt_to_equity is "lower is better" so it gets
# inverted in scoring; current_ratio higher is better up to a point.
_BALANCE_METRICS = ("debt_to_equity", "current_ratio")

_CASH_FLOW_METRICS = ("free_cash_flow_yield", "operating_cash_flow_margin")


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------

@dataclass
class FundamentalScores:
    ticker: str
    growth_score: float = 50.0
    quality_score: float = 50.0
    valuation_score: float = 50.0
    balance_sheet_score: float = 50.0
    cash_flow_score: float = 50.0
    fundamentals_score: float = 50.0
    missing_fields: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DataFrame construction
# ---------------------------------------------------------------------------

def fundamentals_dataframe(records: Iterable[Fundamentals]) -> pd.DataFrame:
    """Flatten a list of Fundamentals into a DataFrame indexed by ticker."""
    rows: List[dict] = []
    for f in records:
        row = dict(f.__dict__)
        row.pop("missing_fields", None)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("ticker", drop=False)
    return df


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_metrics(
    df: pd.DataFrame,
    metric_columns: List[str],
    cfg: ScoringConfig,
) -> pd.DataFrame:
    """Return a DataFrame of [0, 100] scores per metric.

    For each metric we:
      1. winsorize at the config percentiles,
      2. z-score across non-missing values,
      3. invert the sign for "lower is better" metrics,
      4. map z -> 0..100 via 50 + 15·z, clipped.
    """
    out = pd.DataFrame(index=df.index)
    for col in metric_columns:
        if col not in df.columns:
            out[col] = np.nan
            continue
        series = pd.to_numeric(df[col], errors="coerce")
        if series.notna().sum() < 2:
            # Not enough data to z-score; return neutral 50 where present.
            out[col] = series.notna().map({True: 50.0, False: np.nan})
            continue

        winsor = _winsorize(series, cfg.winsor_lower_pct, cfg.winsor_upper_pct)
        mu = winsor.mean(skipna=True)
        sd = winsor.std(skipna=True)
        if not math.isfinite(sd) or sd == 0:
            out[col] = winsor.notna().map({True: 50.0, False: np.nan})
            continue

        z = (winsor - mu) / sd
        if col in _INVERTED_METRICS:
            z = -z
        score = 50.0 + 15.0 * z
        out[col] = score.clip(lower=0.0, upper=100.0)
    return out


def _winsorize(series: pd.Series, lower_pct: float, upper_pct: float) -> pd.Series:
    if series.notna().sum() == 0:
        return series
    lo = series.quantile(lower_pct)
    hi = series.quantile(upper_pct)
    if pd.isna(lo) or pd.isna(hi):
        return series
    return series.clip(lower=lo, upper=hi)


# ---------------------------------------------------------------------------
# Pillar aggregation
# ---------------------------------------------------------------------------

def _weighted_pillar_score(
    metric_scores: pd.Series,
    weights: Dict[str, float],
    missing_penalty_per_field: float,
) -> Tuple[float, int]:
    """Combine metric-level [0,100] scores into a single pillar score.

    Weights are renormalized over the metrics for which we have data, so
    a single missing field doesn't crater the score by itself. But a
    per-field missing penalty is applied multiplicatively at the end to
    discourage tickers with thin disclosure from sneaking through.

    Returns (score, missing_count).
    """
    if not weights:
        return 50.0, 0
    present_weight = 0.0
    weighted_sum = 0.0
    missing_count = 0
    for metric, w in weights.items():
        v = metric_scores.get(metric, np.nan)
        if pd.isna(v):
            missing_count += 1
            continue
        present_weight += w
        weighted_sum += w * float(v)
    if present_weight <= 0:
        return 50.0, missing_count
    raw = weighted_sum / present_weight
    penalty = 1.0 - missing_penalty_per_field * missing_count
    penalty = max(0.0, min(1.0, penalty))
    return float(max(0.0, min(100.0, raw * penalty))), missing_count


# ---------------------------------------------------------------------------
# Top-level scoring
# ---------------------------------------------------------------------------

def compute_pillar_scores(
    df: pd.DataFrame,
    cfg: ScoringConfig,
) -> pd.DataFrame:
    """Compute growth/quality/valuation/balance_sheet/cash_flow scores per ticker.

    Returns a DataFrame indexed by ticker with one column per pillar.
    """
    if df.empty:
        return pd.DataFrame()

    pillar_definitions = {
        "growth": (list(GROWTH_METRICS), cfg.growth),
        "quality": (list(QUALITY_METRICS), cfg.quality),
        "valuation": (list(VALUATION_METRICS), cfg.valuation),
        "balance_sheet": (list(_BALANCE_METRICS), cfg.balance_sheet),
        "cash_flow": (list(_CASH_FLOW_METRICS), cfg.cash_flow),
    }

    pillar_scores = pd.DataFrame(index=df.index)
    missing_count = pd.Series(0, index=df.index, dtype=int)
    # Collect every metric's normalized [0,100] score so we can name the
    # single strongest / weakest metric per ticker (explainability, Issue 6).
    all_metric_scores = pd.DataFrame(index=df.index)

    for pillar, (metrics, weights) in pillar_definitions.items():
        metric_scores = normalize_metrics(df, metrics, cfg)
        for col in metric_scores.columns:
            all_metric_scores[col] = metric_scores[col]
        scores: List[float] = []
        missing_list: List[int] = []
        for ticker in df.index:
            score, miss = _weighted_pillar_score(
                metric_scores.loc[ticker],
                weights or {},
                cfg.missing_penalty_per_field,
            )
            scores.append(score)
            missing_list.append(miss)
        pillar_scores[f"{pillar}_score"] = scores
        missing_count = missing_count + pd.Series(missing_list, index=df.index)

    pillar_scores["missing_metric_count"] = missing_count
    _attach_explainability(pillar_scores, df, all_metric_scores)
    return pillar_scores


def _attach_explainability(
    pillar_scores: pd.DataFrame,
    df: pd.DataFrame,
    all_metric_scores: pd.DataFrame,
) -> None:
    """Add per-ticker explainability columns in place.

    * strongest_metric / strongest_metric_score -- best individual metric
    * weakest_metric   / weakest_metric_score   -- worst individual metric
    * market_cap_available  -- did the provider return a market cap?
    * valuation_metrics_available -- how many of the 5 valuation ratios exist
    Naming the best/worst metric lets a reader see *why* a fundamentals score
    is what it is instead of trusting an opaque number.
    """
    strongest: List[Optional[str]] = []
    strongest_val: List[Optional[float]] = []
    weakest: List[Optional[str]] = []
    weakest_val: List[Optional[float]] = []
    for ticker in df.index:
        row = all_metric_scores.loc[ticker].dropna() if len(all_metric_scores.columns) else pd.Series(dtype=float)
        if row.empty:
            strongest.append(None); strongest_val.append(None)
            weakest.append(None); weakest_val.append(None)
            continue
        s_name = row.idxmax(); w_name = row.idxmin()
        strongest.append(str(s_name)); strongest_val.append(round(float(row[s_name]), 1))
        weakest.append(str(w_name)); weakest_val.append(round(float(row[w_name]), 1))

    pillar_scores["strongest_metric"] = strongest
    pillar_scores["strongest_metric_score"] = strongest_val
    pillar_scores["weakest_metric"] = weakest
    pillar_scores["weakest_metric_score"] = weakest_val

    if "market_cap" in df.columns:
        pillar_scores["market_cap_available"] = pd.to_numeric(
            df["market_cap"], errors="coerce"
        ).notna().values
    else:
        pillar_scores["market_cap_available"] = False

    present_val = pd.Series(0, index=df.index, dtype=int)
    for col in VALUATION_METRICS:
        if col in df.columns:
            present_val = present_val + pd.to_numeric(df[col], errors="coerce").notna().astype(int)
    pillar_scores["valuation_metrics_available"] = present_val.values


def score_fundamentals(
    records: Iterable[Fundamentals],
    cfg: ScoringConfig,
) -> pd.DataFrame:
    """End-to-end: raw fundamentals -> pillar scores + fundamentals_score.

    Output columns: ticker, growth_score, quality_score, valuation_score,
    balance_sheet_score, cash_flow_score, fundamentals_score,
    missing_metric_count.
    """
    df = fundamentals_dataframe(records)
    if df.empty:
        return pd.DataFrame()
    pillars = compute_pillar_scores(df, cfg)

    pw = cfg.pillars or {}
    total_w = sum(pw.values()) or 1.0
    fundamentals_score = sum(
        (pw.get(name, 0.0) / total_w) * pillars[f"{name}_score"]
        for name in ("growth", "quality", "valuation", "balance_sheet", "cash_flow")
    )
    pillars["fundamentals_score"] = fundamentals_score.clip(lower=0.0, upper=100.0)
    pillars["ticker"] = pillars.index
    return pillars.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Convenience: build a Fundamentals -> all-pillar extracts dictionary
# (mostly useful for tests / inspection)
# ---------------------------------------------------------------------------

def explain_record(f: Fundamentals) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    out.update(extract_growth_metrics(f))
    out.update(extract_quality_metrics(f))
    out.update(extract_valuation_metrics(f))
    out["debt_to_equity"] = f.debt_to_equity
    out["current_ratio"] = f.current_ratio
    out["free_cash_flow_yield"] = f.free_cash_flow_yield
    out["operating_cash_flow_margin"] = f.operating_cash_flow_margin
    return out
