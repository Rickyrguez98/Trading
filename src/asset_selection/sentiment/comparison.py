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
FLAG_FINBERT_SCORING_ERROR = "FINBERT_SCORING_ERROR"
FLAG_SENTIMENT_MODEL_FALLBACK = "SENTIMENT_MODEL_FALLBACK"
FLAG_ENSEMBLE_SENTIMENT = "ENSEMBLE_SENTIMENT"

# --- Agreement categories (richer than a binary agree/disagree) --------------
# Two models can "agree" in three different directions, "disagree" by a little or
# a lot, or one model can be missing -- each is a materially different read.
AGREEMENT_POSITIVE = "agreement_positive"
AGREEMENT_NEUTRAL = "agreement_neutral"
AGREEMENT_NEGATIVE = "agreement_negative"
AGREEMENT_MILD_DISAGREEMENT = "mild_disagreement"
AGREEMENT_STRONG_DISAGREEMENT = "strong_disagreement"
AGREEMENT_FINBERT_UNAVAILABLE = "finbert_unavailable"
AGREEMENT_VADER_ONLY = "vader_only"

AGREEMENT_CATEGORIES = (
    AGREEMENT_POSITIVE,
    AGREEMENT_NEUTRAL,
    AGREEMENT_NEGATIVE,
    AGREEMENT_MILD_DISAGREEMENT,
    AGREEMENT_STRONG_DISAGREEMENT,
    AGREEMENT_FINBERT_UNAVAILABLE,
    AGREEMENT_VADER_ONLY,
)

# Scores within this band of ``neutral`` (50) read as "neutral" direction.
_DIRECTION_BAND = 5.0


def _direction(score: float, neutral: float = 50.0, band: float = _DIRECTION_BAND) -> str:
    if score >= neutral + band:
        return "positive"
    if score <= neutral - band:
        return "negative"
    return "neutral"


def _classify_agreement(
    vader_score: Optional[float],
    finbert_score: Optional[float],
    *,
    strong_threshold: float,
    finbert_attempted: bool,
    neutral: float = 50.0,
) -> str:
    """Bucket a ticker into one of the seven agreement categories.

    When FinBERT produced no score we distinguish *finbert_unavailable* (it was
    wanted but couldn't run) from *vader_only* (a plain VADER run that never asked
    for FinBERT). When both scored, a delta at/above ``strong_threshold`` is a
    strong disagreement; at/above half that is a mild disagreement; otherwise the
    two agree and we label the shared direction.
    """
    if finbert_score is None or vader_score is None:
        return AGREEMENT_FINBERT_UNAVAILABLE if finbert_attempted else AGREEMENT_VADER_ONLY

    delta = abs(vader_score - finbert_score)
    if delta >= strong_threshold:
        return AGREEMENT_STRONG_DISAGREEMENT
    if delta >= strong_threshold / 2.0:
        return AGREEMENT_MILD_DISAGREEMENT

    mean_score = (vader_score + finbert_score) / 2.0
    direction = _direction(mean_score, neutral=neutral)
    if direction == "positive":
        return AGREEMENT_POSITIVE
    if direction == "negative":
        return AGREEMENT_NEGATIVE
    return AGREEMENT_NEUTRAL


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
    # Which score the composite ultimately consumes: vader | finbert | ensemble.
    final_sentiment_source: str = "vader"
    finbert_model_name: Optional[str] = None
    # Concrete device a real FinBERT loaded onto (cpu/cuda/mps); None if not loaded.
    finbert_device_used: Optional[str] = None

    @property
    def finbert_usable(self) -> bool:
        return self.finbert_model is not None

    @property
    def ensemble_mode(self) -> bool:
        return self.model_name == "ensemble" or self.final_sentiment_source == "ensemble"


def build_sentiment_runtime(cfg: SentimentConfig) -> SentimentRuntime:
    """Resolve VADER (always) and FinBERT (optional) without ever crashing.

    FinBERT is *attempted* when the config asks for it -- ``model`` is
    ``finbert``/``comparison``, ``compare_models`` is true, or ``finbert_enabled``
    is set. If it cannot be loaded we degrade to VADER-only and record an honest
    reason; we never fabricate a model.
    """
    model_name = (cfg.model or "vader").lower()
    # ``comparison`` AND ``ensemble`` both score with BOTH backends side by side.
    comparison_mode = bool(cfg.compare_models) or model_name in ("comparison", "ensemble")
    want_finbert = (
        comparison_mode
        or model_name in ("finbert", "ensemble")
        or bool(getattr(cfg, "finbert_enabled", False))
    )
    # ``ensemble`` forces the blended final source regardless of the config knob.
    final_sentiment_source = (
        "ensemble" if model_name == "ensemble" else (cfg.final_sentiment_source or "vader").lower()
    )

    vader_model = VaderSentimentModel()
    finbert_model: Optional[SentimentModel] = None
    deps_present = is_finbert_available()
    finbert_loaded = False
    finbert_attempted = False
    finbert_device_used: Optional[str] = None
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
                finbert_model = FinBertSentimentModel(
                    cfg.finbert_model_name,
                    batch_size=cfg.finbert_batch_size,
                    max_length=cfg.finbert_max_length,
                    device=cfg.finbert_device,
                )
                finbert_loaded = True
                finbert_device_used = getattr(finbert_model, "device_used", None)
                logger.info(
                    "Sentiment: FinBERT loaded (%s) on %s.",
                    cfg.finbert_model_name,
                    finbert_device_used,
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
        final_sentiment_source=final_sentiment_source,
        finbert_model_name=cfg.finbert_model_name,
        finbert_device_used=finbert_device_used,
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
            # Carry FinBERT's class distribution + any scoring error onto the
            # representative (VADER) record so a single article row can show
            # BOTH models without re-scoring.
            va.finbert_positive_probability = fb.finbert_positive_probability
            va.finbert_neutral_probability = fb.finbert_neutral_probability
            va.finbert_negative_probability = fb.finbert_negative_probability
            if fb.scoring_error and not va.scoring_error:
                va.scoring_error = fb.scoring_error
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

    strong_threshold = cfg.disagreement_threshold
    delta = abs(vader_score - finbert_score) if finbert_score is not None else None
    agreement = _classify_agreement(
        vader_score,
        finbert_score,
        strong_threshold=strong_threshold,
        finbert_attempted=runtime.finbert_attempted,
        neutral=cfg.neutral_sentiment_score,
    )
    # The SENTIMENT_MODEL_DISAGREEMENT flag fires only on a *strong* disagreement
    # (a mild one is informational, not a red flag).
    if agreement == AGREEMENT_STRONG_DISAGREEMENT:
        flags.append(FLAG_DISAGREEMENT)

    # FINBERT_SCORING_ERROR: a real FinBERT captured (not raised) a per-article
    # failure -- surface it instead of silently treating it as neutral signal.
    finbert_error_count = sum(1 for a in finbert_scored if a.scoring_error)
    if finbert_error_count:
        flags.append(FLAG_FINBERT_SCORING_ERROR)

    # LOW_FINBERT_CONFIDENCE: mean per-article FinBERT polarity magnitude is the
    # model's own confidence; a near-zero mean means FinBERT is "not sure".
    if finbert_scored:
        mean_conf = sum(abs(a.compound) for a in finbert_scored) / len(finbert_scored)
        if mean_conf < cfg.low_finbert_confidence_threshold:
            flags.append(FLAG_LOW_FINBERT_CONFIDENCE)

    # --- Final-score selection (improvement #6). -----------------------------
    # This only chooses WHICH sentiment number (0..100) feeds the single capped
    # sentiment column; fundamentals still dominate the composite downstream.
    fallback_used = False
    if comparison_mode:
        # ``ensemble`` mode forces the blended source regardless of the knob.
        effective_source = (
            "ensemble" if runtime.model_name == "ensemble"
            else (cfg.final_sentiment_source or "vader").lower()
        )
        if finbert_agg is None:
            flags += [FLAG_FINBERT_UNAVAILABLE, FLAG_VADER_ONLY]
            if effective_source in ("finbert", "ensemble"):
                fallback_used = True  # wanted FinBERT/ensemble, forced to VADER
            final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"
        elif effective_source == "finbert":
            final_score, final_conf, used = finbert_score, finbert_agg.confidence, "finbert"
        elif effective_source == "ensemble":
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
            fallback_used = True
            final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"
        else:
            # No fallback allowed: report neutral, explicitly NOT fabricated.
            flags.append(FLAG_FINBERT_UNAVAILABLE)
            final_score, final_conf, used = cfg.neutral_sentiment_score, 0.0, "none"
    else:  # plain VADER (default)
        final_score, final_conf, used = vader_score, vader_agg.confidence, "vader"

    if used == "ensemble":
        flags.append(FLAG_ENSEMBLE_SENTIMENT)
    if fallback_used:
        flags.append(FLAG_SENTIMENT_MODEL_FALLBACK)

    rep = vader_agg  # feed metrics are model-independent

    # Mean FinBERT class probabilities across the ticker's scored articles (only
    # meaningful when a real FinBERT ran; None otherwise -- never fabricated).
    finbert_pos_mean = _mean([a.finbert_positive_probability for a in finbert_scored])
    finbert_neu_mean = _mean([a.finbert_neutral_probability for a in finbert_scored])
    finbert_neg_mean = _mean([a.finbert_negative_probability for a in finbert_scored])

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
        "finbert_device_used": runtime.finbert_device_used,
        "sentiment_model_fallback_used": fallback_used,
        "finbert_scoring_error_count": finbert_error_count,
        "finbert_positive_probability": finbert_pos_mean,
        "finbert_neutral_probability": finbert_neu_mean,
        "finbert_negative_probability": finbert_neg_mean,
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
        "_finbert_scoring_errors": finbert_error_count,
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
    finbert_errors = sum(int(r.get("_finbert_scoring_errors", 0) or 0) for r in rows)

    # Agreement breakdown across the seven categories (missing categories are 0).
    agreement_counter = Counter(
        r.get("sentiment_model_agreement") for r in rows
        if r.get("sentiment_model_agreement")
    )
    agreement_breakdown = {cat: int(agreement_counter.get(cat, 0)) for cat in AGREEMENT_CATEGORIES}
    strong = [
        r.get("ticker") for r in rows
        if r.get("sentiment_model_agreement") == AGREEMENT_STRONG_DISAGREEMENT
    ]
    mild_count = agreement_breakdown[AGREEMENT_MILD_DISAGREEMENT]

    used_counter = Counter(
        r.get("sentiment_model_used") for r in rows if r.get("sentiment_model_used")
    )
    dominant_used = used_counter.most_common(1)[0][0] if used_counter else runtime.model_name
    fallback_used = any(bool(r.get("sentiment_model_fallback_used")) for r in rows)

    notes: List[str] = []
    if runtime.comparison_mode and not runtime.finbert_usable:
        notes.append(FLAG_FINBERT_UNAVAILABLE)
    if runtime.finbert_unavailable_reason:
        notes.append(runtime.finbert_unavailable_reason)
    if finbert_errors:
        notes.append(f"{finbert_errors} article(s) hit a FinBERT scoring error.")

    avg_v = _mean(vader_scores)
    avg_f = _mean(finbert_scores)
    return {
        "configured_model": runtime.model_name,
        "comparison_mode": runtime.comparison_mode,
        "final_sentiment_source": runtime.final_sentiment_source,
        "sentiment_model_used": dominant_used,
        "finbert_available": runtime.finbert_usable,
        "finbert_deps_present": runtime.finbert_deps_present,
        "finbert_attempted": runtime.finbert_attempted,
        "finbert_unavailable_reason": runtime.finbert_unavailable_reason,
        "finbert_model_name": runtime.finbert_model_name,
        "finbert_device_used": runtime.finbert_device_used,
        "fallback_to_vader_used": fallback_used,
        "articles_scored_vader": articles_vader,
        "articles_scored_finbert": articles_finbert,
        "finbert_scoring_error_count": finbert_errors,
        "avg_vader_sentiment_score": round(avg_v, 2) if avg_v is not None else None,
        "avg_finbert_sentiment_score": round(avg_f, 2) if avg_f is not None else None,
        "sentiment_model_disagreement_count": len(strong),
        "mild_disagreement_count": mild_count,
        "agreement_breakdown": agreement_breakdown,
        "tickers_with_large_disagreement": [t for t in strong if t][:50],
        "notes": notes,
    }
