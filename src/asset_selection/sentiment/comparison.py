"""VADER vs FinBERT comparison engine.

This lets the pipeline run the finance-tuned model (FinBERT) alongside the
default lexicon model (VADER) and compare them per ticker **without ever
fabricating a FinBERT result**.

Design rules (enforced by the tests):

* VADER always works -- no extra dependencies.
* FinBERT is optional. If ``transformers``/``torch`` are not installed, or the
  model fails to load (download/offline/OOM), the engine degrades to VADER-only
  and *says so* via explicit flags. It never invents a FinBERT number.
* The composite still consumes a single ``sentiment_score`` column, kept on the
  0..100 scale, so fundamentals continue to dominate by default.

The public surface is:

* :func:`is_finbert_available` -- pure import probe (loads no model).
* :func:`build_sentiment_runtime` -- resolve VADER (+ optional FinBERT) once.
* :func:`resolve_ticker_sentiment` -- score one ticker (single OR comparison).
* :func:`build_run_sentiment_summary` -- run-level reporting block.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ..config import SentimentConfig
from ..data_providers.base import NewsItem
from .sentiment_model import (
    ArticleSentiment,
    FinBertSentimentModel,
    SentimentModel,
    VaderSentimentModel,
    aggregate_ticker_sentiment,
    score_articles,
)

logger = logging.getLogger(__name__)

# --- Comparison flags (also merged into the per-row ``flags`` list) ----------
FLAG_DISAGREEMENT = "SENTIMENT_MODEL_DISAGREEMENT"
FLAG_FINBERT_UNAVAILABLE = "FINBERT_UNAVAILABLE"
FLAG_VADER_ONLY = "VADER_ONLY_SENTIMENT"
FLAG_LOW_FINBERT_CONFIDENCE = "LOW_FINBERT_CONFIDENCE"


def is_finbert_available() -> bool:
    """Return True iff FinBERT's dependencies import.

    This is a *pure import probe*: it loads no model, makes no network call, and
    never raises. It only answers "is attempting FinBERT even possible?" -- it
    must NEVER be used to manufacture a score.
    """
    try:
        import importlib.util

        return (
            importlib.util.find_spec("transformers") is not None
            and importlib.util.find_spec("torch") is not None
        )
    except Exception:  # noqa: BLE001 - a broken import system must read as "no"
        return False


# ---------------------------------------------------------------------------
# Runtime: resolve the model(s) once, never crash
# ---------------------------------------------------------------------------

@dataclass
class SentimentRuntime:
    """Resolved sentiment backends for a run.

    Built once by :func:`build_sentiment_runtime` so the per-ticker loop never
    re-probes or re-loads. ``finbert_model is None`` means FinBERT is not usable
    (deps missing or load failed); downstream code reads :attr:`finbert_usable`.
    """

    vader_model: SentimentModel
    finbert_model: Optional[SentimentModel]
    comparison_mode: bool
    finbert_deps_present: bool
    finbert_loaded: bool
    finbert_attempted: bool
    model_name: str
    finbert_unavailable_reason: Optional[str] = None

    @property
    def finbert_usable(self) -> bool:
        return self.finbert_model is not None


def build_sentiment_runtime(cfg: SentimentConfig) -> SentimentRuntime:
    """Resolve VADER (always) and FinBERT (optional) without ever crashing.

    FinBERT is *attempted* when the config asks for it -- ``model`` is
    ``finbert``/``comparison``, ``compare_models`` is true, or ``finbert_enabled``
    is set. If it cannot be loaded we degrade to VADER-only and record an honest
    reason; we never fabricate a model.
    """
    model_name = (cfg.model or "vader").lower()
    comparison_mode = bool(cfg.compare_models) or model_name == "comparison"
    want_finbert = (
        comparison_mode
        or model_name == "finbert"
        or bool(getattr(cfg, "finbert_enabled", False))
    )

    vader_model = VaderSentimentModel()
    finbert_model: Optional[SentimentModel] = None
    deps_present = is_finbert_available()
    finbert_loaded = False
    finbert_attempted = False
    reason: Optional[str] = None

    if want_finbert:
        finbert_attempted = True
        if not deps_present:
            reason = (
                "FinBERT requested but transformers/torch are not installed; "
                "install with: pip install '.[finbert]'"
            )
            logger.warning("Sentiment: %s -- degrading to VADER.", reason)
        else:
            try:
                finbert_model = FinBertSentimentModel(cfg.finbert_model_name)
                finbert_loaded = True
                logger.info(
                    "Sentiment: FinBERT loaded (%s).", cfg.finbert_model_name
                )
            except Exception as exc:  # noqa: BLE001 - never fabricate; degrade
                reason = f"FinBERT failed to load: {type(exc).__name__}: {exc}"
                finbert_model = None
                logger.warning("Sentiment: %s -- degrading to VADER.", reason)

    return SentimentRuntime(
        vader_model=vader_model,
        finbert_model=finbert_model,
        comparison_mode=comparison_mode,
        finbert_deps_present=deps_present,
        finbert_loaded=finbert_loaded,
        finbert_attempted=finbert_attempted,
        model_name=model_name,
        finbert_unavailable_reason=reason,
    )


# ---------------------------------------------------------------------------
# Per-ticker resolution
# ---------------------------------------------------------------------------

def _agg_params(cfg: SentimentConfig) -> Dict[str, Any]:
    """Model-independent aggregation knobs shared by both backends."""
    return {
        "recency_halflife_days": cfg.recency_halflife_days,
        "min_articles_for_confidence": cfg.min_articles_for_confidence,
        "confidence_full_article_count": cfg.confidence_full_article_count,
        "confidence_full_source_count": cfg.confidence_full_source_count,
        "stale_after_days": cfg.stale_after_days,
    }


def _attach_dual_scores(
    vader_scored: Sequence[ArticleSentiment],
    finbert_scored: Sequence[ArticleSentiment],
) -> None:
    """Annotate per-article records with BOTH models' scores/labels.

    ``vader_scored`` and ``finbert_scored`` are positionally aligned: both come
    from the same ``articles`` list filtered by the same ``clean_text`` rule, so
    index *i* refers to the same article. We only zip up to the shorter length.
    """
    for i, va in enumerate(vader_scored):
        va.vader_score = va.compound
        va.vader_label = va.label
        va.model_used = "vader"
        if i < len(finbert_scored):
            fb = finbert_scored[i]
            va.finbert_score = fb.compound
            va.finbert_label = fb.label
            fb.vader_score = va.compound
            fb.vader_label = va.label
            fb.finbert_score = fb.compound
            fb.finbert_label = fb.label
            fb.model_used = "finbert"


def resolve_ticker_sentiment(
    ticker: str,
    articles: Sequence[NewsItem],
    runtime: SentimentRuntime,
    cfg: SentimentConfig,
) -> Dict[str, Any]:
    """Score one ticker and return a normalized row dict.

    Works for single-model and comparison runs alike. Keys prefixed with ``_``
    are private bookkeeping for :func:`build_run_sentiment_summary` and are
    dropped before the row reaches the DataFrame.

    FinBERT is never fabricated: if it is not usable, ``finbert_sentiment_score``
    is ``None`` and the appropriate flags are set.
    """
    agg = _agg_params(cfg)
    flags: List[str] = []

    # --- VADER: always scored; supplies model-independent feed metrics. ------
    vader_scored = score_articles(articles, runtime.vader_model)
    vader_agg = aggregate_ticker_sentiment(
        ticker, vader_scored,
        model_confidence_factor=cfg.vader_confidence_factor,
        model_name="vader", **agg,
    )

    # --- FinBERT: optional. Only scored when a real model is loaded. ---------
    finbert_agg = None
    finbert_scored: List[ArticleSentiment] = []
    if runtime.finbert_usable:
        finbert_scored = score_articles(articles, runtime.finbert_model)
        finbert_agg = aggregate_ticker_sentiment(
            ticker, finbert_scored,
            model_confidence_factor=cfg.finbert_confidence_factor,
            model_name="finbert", **agg,
        )

    _attach_dual_scores(vader_scored, finbert_scored)

    comparison_mode = runtime.comparison_mode
    single_finbert = runtime.model_name == "finbert" and not comparison_mode

    vader_score = vader_agg.sentiment_score
    finbert_score = finbert_agg.sentiment_score if finbert_agg else None
    delta = abs(vader_score - finbert_score) if finbert_score is not None else None
    agreement: Optional[str] = None
    if delta is not None:
        agreement = "agree" if delta <= cfg.sentiment_disagreement_threshold else "disagree"
        if agreement == "disagree":
            flags.append(FLAG_DISAGREEMENT)

    # LOW_FINBERT_CONFIDENCE: mean per-article FinBERT polarity magnitude is the
    # model's own confidence; a near-zero mean means FinBERT is "not sure".
    if finbert_scored:
        mean_conf = sum(abs(a.compound) for a in finbert_scored) / len(finbert_scored)
        if mean_conf < cfg.low_finbert_confidence_threshold:
            flags.append(FLAG_LOW_FINBERT_CONFIDENCE)

    # --- Final-score selection (improvement #6). -----------------------------
    if comparison_mode:
        if finbert_agg is None:
            flags += [FLAG_FINBERT_UNAVAILABLE, FLAG_VADER_ONLY]
            final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"
        else:
            source = (cfg.final_sentiment_source or "vader").lower()
            if source == "finbert":
                final_score, final_conf, used = (
                    finbert_score, finbert_agg.confidence, "finbert",
                )
            elif source == "ensemble":
                wv = float(cfg.ensemble_vader_weight)
                wf = float(cfg.ensemble_finbert_weight)
                tot = (wv + wf) or 1.0
                final_score = (wv * vader_score + wf * finbert_score) / tot
                final_conf = (wv * vader_agg.confidence + wf * finbert_agg.confidence) / tot
                used = "ensemble"
            else:
                final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"
    elif single_finbert:
        if finbert_agg is not None:
            final_score, final_conf, used = finbert_score, finbert_agg.confidence, "finbert"
        elif cfg.fallback_to_vader_if_finbert_unavailable:
            flags += [FLAG_FINBERT_UNAVAILABLE, FLAG_VADER_ONLY]
            final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"
        else:
            # No fallback allowed: report neutral, explicitly NOT fabricated.
            flags.append(FLAG_FINBERT_UNAVAILABLE)
            final_score, final_conf, used = cfg.neutral_sentiment_score, 0.0, "none"
    else:  # plain VADER (default)
        final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"

    rep = vader_agg  # feed metrics are model-independent
    return {
        "ticker": ticker,
        # The single column the composite consumes (0..100):
        "sentiment_score": final_score,
        "sentiment_confidence": final_conf,
        "final_sentiment_score": final_score,
        "sentiment_model": used,
        "sentiment_model_used": used,
        # Per-model comparison fields (improvement #4):
        "vader_sentiment_score": vader_score,
        "finbert_sentiment_score": finbert_score,
        "vader_sentiment_confidence": vader_agg.confidence,
        "finbert_sentiment_confidence": finbert_agg.confidence if finbert_agg else None,
        "sentiment_score_delta": delta,
        "sentiment_model_agreement": agreement,
        "sentiment_flags": flags,
        "finbert_available": runtime.finbert_usable,
        # Model-independent feed metrics:
        "article_count": rep.article_count,
        "unique_article_count": rep.unique_article_count,
        "duplicate_count": rep.duplicate_count,
        "stale_count": rep.stale_count,
        "fresh_ratio": rep.fresh_ratio,
        "unique_ratio": rep.unique_ratio,
        "positive_ratio": rep.positive_ratio,
        "negative_ratio": rep.negative_ratio,
        "neutral_ratio": rep.neutral_ratio,
        "source_diversity": rep.source_diversity,
        "news_titles": [a.headline for a in rep.articles[:10]],
        # Private bookkeeping for the run-level summary:
        "_vader_articles_scored": len(vader_scored),
        "_finbert_articles_scored": len(finbert_scored),
    }


# ---------------------------------------------------------------------------
# Run-level summary (improvement #7)
# ---------------------------------------------------------------------------

def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def build_run_sentiment_summary(
    runtime: SentimentRuntime,
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Roll the per-ticker rows into a run-level sentiment reporting block."""
    vader_scores = [r.get("vader_sentiment_score") for r in rows]
    finbert_scores = [r.get("finbert_sentiment_score") for r in rows]
    articles_vader = sum(int(r.get("_vader_articles_scored", 0) or 0) for r in rows)
    articles_finbert = sum(int(r.get("_finbert_articles_scored", 0) or 0) for r in rows)
    disagree = [
        r.get("ticker") for r in rows
        if r.get("sentiment_model_agreement") == "disagree"
    ]
    used_counter = Counter(
        r.get("sentiment_model_used") for r in rows if r.get("sentiment_model_used")
    )
    dominant_used = used_counter.most_common(1)[0][0] if used_counter else runtime.model_name

    notes: List[str] = []
    if runtime.comparison_mode and not runtime.finbert_usable:
        notes.append(FLAG_FINBERT_UNAVAILABLE)
    if runtime.finbert_unavailable_reason:
        notes.append(runtime.finbert_unavailable_reason)

    avg_v = _mean(vader_scores)
    avg_f = _mean(finbert_scores)
    return {
        "configured_model": runtime.model_name,
        "comparison_mode": runtime.comparison_mode,
        "sentiment_model_used": dominant_used,
        "finbert_available": runtime.finbert_usable,
        "finbert_deps_present": runtime.finbert_deps_present,
        "finbert_attempted": runtime.finbert_attempted,
        "finbert_unavailable_reason": runtime.finbert_unavailable_reason,
        "articles_scored_vader": articles_vader,
        "articles_scored_finbert": articles_finbert,
        "avg_vader_sentiment_score": round(avg_v, 2) if avg_v is not None else None,
        "avg_finbert_sentiment_score": round(avg_f, 2) if avg_f is not None else None,
        "sentiment_model_disagreement_count": len(disagree),
        "tickers_with_large_disagreement": [t for t in disagree if t][:50],
        "notes": notes,
    }
