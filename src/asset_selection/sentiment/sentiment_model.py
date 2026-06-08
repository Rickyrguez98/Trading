"""Sentiment scoring with a pluggable backend.

- :class:`VaderSentimentModel` — default; lexicon-based; no extra deps.
- :class:`FinBertSentimentModel` — optional; requires ``transformers`` + ``torch``
  (install with ``pip install '.[finbert]'``).

We score article-by-article and then aggregate per ticker with recency weighting.
The aggregate output is on a 0..100 scale to match the rest of the scoring system.
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Sequence

from ..data_providers.base import NewsItem
from .text_preprocessing import clean_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScoreResult:
    """One article's polarity plus (optionally) the model's class probabilities.

    ``compound`` is always on [-1, +1] (positive_probability - negative_probability
    for FinBERT; VADER's compound for VADER). The probability fields are populated
    only by a model that emits a real class distribution (FinBERT). ``error`` holds
    a captured scoring failure so a single bad record never aborts a batch and is
    never silently fabricated as a real score.
    """
    compound: float
    positive_probability: Optional[float] = None
    neutral_probability: Optional[float] = None
    negative_probability: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ArticleSentiment:
    """Sentiment of a single article, normalized to [-1, +1]."""
    ticker: str
    headline: str
    compound: float                  # -1..+1
    label: str                       # 'positive' | 'neutral' | 'negative'
    source: Optional[str] = None
    published_at: Optional[str] = None
    url: Optional[str] = None
    retrieved_at: Optional[str] = None
    is_duplicate: bool = False       # same headline/url already seen for this ticker
    is_stale: bool = False           # older than the configured staleness window
    age_days: Optional[float] = None
    # --- Per-article dual-model comparison (populated only in comparison runs) ---
    # ``compound``/``label`` above always hold the score of the model that scored
    # THIS record; the fields below let one record carry BOTH models' views so the
    # report can show vader vs finbert side by side without re-scoring.
    vader_score: Optional[float] = None
    finbert_score: Optional[float] = None
    vader_label: Optional[str] = None
    finbert_label: Optional[str] = None
    model_used: Optional[str] = None
    # FinBERT class probabilities (populated only when a real FinBERT scored this
    # record; None for VADER-only records -- never fabricated).
    finbert_positive_probability: Optional[float] = None
    finbert_neutral_probability: Optional[float] = None
    finbert_negative_probability: Optional[float] = None
    # A captured (not raised) per-article scoring failure, if any.
    scoring_error: Optional[str] = None


@dataclass
class TickerSentiment:
    """Aggregated sentiment for a ticker, on a 0..100 scale."""
    ticker: str
    sentiment_score: float = 50.0    # 0..100; 50 = neutral
    average_compound: float = 0.0
    recency_weighted_compound: float = 0.0
    article_count: int = 0
    unique_article_count: int = 0
    duplicate_count: int = 0
    stale_count: int = 0
    fresh_ratio: float = 0.0         # share of non-stale articles
    unique_ratio: float = 0.0        # share of non-duplicate articles
    positive_ratio: float = 0.0
    negative_ratio: float = 0.0
    neutral_ratio: float = 0.0
    source_diversity: int = 0
    confidence: float = 0.0          # 0..1
    model_name: str = "vader"
    articles: List[ArticleSentiment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class SentimentModel(ABC):
    """Strategy interface so backends are swappable in one config line."""

    @abstractmethod
    def score(self, text: str) -> float:
        """Return a compound polarity score in [-1, +1] for the input string."""

    def score_many(self, texts: Sequence[str]) -> List["ScoreResult"]:
        """Score a batch of texts, returning a :class:`ScoreResult` per item.

        Default implementation calls :meth:`score` once per text and carries no
        class probabilities. Backends with a real batched path (FinBERT) override
        this for throughput and to expose the class distribution. Errors are
        captured per item -- a single bad record never aborts the batch and is
        never fabricated as a real score.
        """
        results: List[ScoreResult] = []
        for text in texts:
            try:
                results.append(ScoreResult(compound=float(self.score(text))))
            except Exception as exc:  # noqa: BLE001 - capture, never fabricate
                results.append(
                    ScoreResult(compound=0.0, error=f"{type(exc).__name__}: {exc}")
                )
        return results


class VaderSentimentModel(SentimentModel):
    """VADER lexicon — general-purpose English sentiment. Light and free."""

    def __init__(self) -> None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        self._analyzer = SentimentIntensityAnalyzer()

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        return float(self._analyzer.polarity_scores(text)["compound"])


def _resolve_finbert_device(torch_module: Any, requested: str) -> str:
    """Resolve a concrete torch device string from a (possibly ``auto``) request.

    ``auto`` prefers CUDA, then Apple-Silicon MPS, then CPU. An explicit request
    is honoured only if that backend is actually available; otherwise it degrades
    to CPU (we never crash because a GPU was asked for but isn't present).

    Pulled out as a module-level pure function (taking ``torch`` as an argument)
    so it can be unit-tested with a fake torch and no real GPU.
    """
    requested = (requested or "auto").strip().lower()

    def _cuda_ok() -> bool:
        try:
            return bool(torch_module.cuda.is_available())
        except Exception:  # noqa: BLE001
            return False

    def _mps_ok() -> bool:
        try:
            mps = getattr(getattr(torch_module, "backends", None), "mps", None)
            return bool(mps is not None and mps.is_available())
        except Exception:  # noqa: BLE001
            return False

    if requested == "cuda":
        return "cuda" if _cuda_ok() else "cpu"
    if requested == "mps":
        return "mps" if _mps_ok() else "cpu"
    if requested == "cpu":
        return "cpu"
    # auto (or anything unrecognized): best available, descending.
    if _cuda_ok():
        return "cuda"
    if _mps_ok():
        return "mps"
    return "cpu"


class FinBertSentimentModel(SentimentModel):
    """FinBERT — finance-tuned transformer. Heavy (~440MB) and slow on CPU.

    Install: ``pip install '.[finbert]'``. Lazy-loaded so the default pipeline
    never pays the import/download cost. We load the model + tokenizer directly
    (no high-level ``pipeline``) so we can:

      * run a real softmax over the logits and expose the full class distribution
        (``positive``/``neutral``/``negative`` probabilities), and
      * compute a signed compound = ``P(positive) - P(negative)`` on [-1, +1],
        which maps cleanly onto the existing ``50 + 50*compound`` 0..100 scale.

    Label order is read from ``model.config.id2label`` rather than hard-coded, so
    a re-labelled checkpoint still maps correctly.
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        *,
        batch_size: int = 8,
        max_length: int = 128,
        device: str = "auto",
    ) -> None:
        try:
            # FinBERT is torch-only by design (requirements-finbert.txt declares
            # torch + transformers, never TensorFlow/Flax). transformers will,
            # however, auto-probe for a TF/Flax backend at import time if one is
            # merely *present* on the path -- and a broken/incompatible TF install
            # can then crash the import of the torch model classes. We never use
            # those backends, so disable the probe (setdefault respects an explicit
            # user override). This must be set BEFORE `import transformers`.
            import os

            os.environ.setdefault("USE_TF", "0")
            os.environ.setdefault("USE_FLAX", "0")

            import torch  # type: ignore[import-not-found]
            from transformers import (  # type: ignore[import-not-found]
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise ImportError(
                "FinBERT requires the [finbert] extras (transformers + torch). "
                "Install with: pip install '.[finbert]'"
            ) from exc

        self._torch = torch
        self.model_name = model_name
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(8, int(max_length))
        self.device_used = _resolve_finbert_device(torch, device)

        self._tokenizer = AutoTokenizer.from_pretrained(model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self._model.to(self.device_used)
        self._model.eval()

        # Map label name -> column index from the model config (don't hard-code).
        id2label = {
            int(k): str(v).lower() for k, v in self._model.config.id2label.items()
        }
        self._label_index = {name: idx for idx, name in id2label.items()}

    def _idx(self, name: str) -> Optional[int]:
        # Tolerate label spellings like 'label_positive' / 'pos'.
        for key, idx in self._label_index.items():
            if name in key:
                return idx
        return None

    def score_batch(self, texts: Sequence[str]) -> List[ScoreResult]:
        """Score a single (already non-empty) batch with one forward pass."""
        torch = self._torch
        enc = self._tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        enc = {k: v.to(self.device_used) for k, v in enc.items()}
        with torch.no_grad():
            logits = self._model(**enc).logits
            probs = torch.nn.functional.softmax(logits, dim=-1)
        probs_list = probs.detach().cpu().tolist()

        pos_i = self._idx("positive")
        neg_i = self._idx("negative")
        neu_i = self._idx("neutral")

        results: List[ScoreResult] = []
        for row in probs_list:
            p_pos = float(row[pos_i]) if pos_i is not None else 0.0
            p_neg = float(row[neg_i]) if neg_i is not None else 0.0
            p_neu = float(row[neu_i]) if neu_i is not None else 0.0
            results.append(
                ScoreResult(
                    compound=p_pos - p_neg,
                    positive_probability=p_pos,
                    neutral_probability=p_neu,
                    negative_probability=p_neg,
                )
            )
        return results

    def score_many(self, texts: Sequence[str]) -> List[ScoreResult]:
        """Batched scoring. Empty texts map to neutral; errors are captured.

        Empty strings are mapped to a neutral result *without* running the model
        (empty-text-safe). Non-empty texts are chunked into ``batch_size`` groups;
        a forward-pass failure on a chunk is captured per item (``error`` set) so
        one bad batch never aborts the run and is never fabricated.
        """
        texts = list(texts)
        results: List[Optional[ScoreResult]] = [None] * len(texts)
        todo = [i for i, t in enumerate(texts) if t and str(t).strip()]
        for i in range(len(texts)):
            if i not in set(todo):  # cheap for small lists; clarity over micro-opt
                results[i] = ScoreResult(
                    compound=0.0,
                    positive_probability=0.0,
                    neutral_probability=1.0,
                    negative_probability=0.0,
                )

        for start in range(0, len(todo), self.batch_size):
            chunk_idx = todo[start : start + self.batch_size]
            chunk_texts = [texts[i] for i in chunk_idx]
            try:
                batch_results = self.score_batch(chunk_texts)
                for i, res in zip(chunk_idx, batch_results):
                    results[i] = res
            except Exception as exc:  # noqa: BLE001 - capture, never fabricate
                err = f"{type(exc).__name__}: {exc}"
                logger.warning("FinBERT batch scoring failed: %s", err)
                for i in chunk_idx:
                    results[i] = ScoreResult(compound=0.0, error=err)

        return [r if r is not None else ScoreResult(compound=0.0) for r in results]

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        return self.score_many([text])[0].compound


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_sentiment_model(name: str = "vader") -> SentimentModel:
    name = (name or "vader").lower()
    if name in ("vader", "comparison", "ensemble"):
        # "comparison"/"ensemble" are resolved by build_sentiment_runtime (which
        # loads BOTH backends); the single-model factory returns the
        # always-available base so callers never crash on a missing extra.
        return VaderSentimentModel()
    if name == "finbert":
        return FinBertSentimentModel()
    raise ValueError(f"Unknown sentiment model: {name!r}")


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_POS_THRESHOLD = 0.05
_NEG_THRESHOLD = -0.05


def _label(compound: float) -> str:
    if compound >= _POS_THRESHOLD:
        return "positive"
    if compound <= _NEG_THRESHOLD:
        return "negative"
    return "neutral"


def score_articles(
    articles: Iterable[NewsItem],
    model: SentimentModel,
) -> List[ArticleSentiment]:
    """Score each article (batched) and return a list of ArticleSentiment.

    Articles whose cleaned headline+summary is empty are skipped. Remaining texts
    are scored via :meth:`SentimentModel.score_many` so a real FinBERT runs them
    in batches and carries class probabilities; VADER falls back to per-text.
    """
    kept: List[NewsItem] = []
    texts: List[str] = []
    for art in articles:
        text = clean_text(" ".join(filter(None, [art.headline, art.summary])))
        if not text:
            continue
        kept.append(art)
        texts.append(text)

    results = model.score_many(texts)

    scored: List[ArticleSentiment] = []
    for art, res in zip(kept, results):
        scored.append(
            ArticleSentiment(
                ticker=art.ticker,
                headline=art.headline,
                compound=res.compound,
                label=_label(res.compound),
                source=art.source,
                published_at=art.published_at,
                url=art.url,
                retrieved_at=art.retrieved_at,
                finbert_positive_probability=res.positive_probability,
                finbert_neutral_probability=res.neutral_probability,
                finbert_negative_probability=res.negative_probability,
                scoring_error=res.error,
            )
        )
    return scored


def _dedup_key(art: ArticleSentiment) -> str:
    """Identity used for duplicate detection.

    Wire stories are frequently re-published verbatim across aggregators, so we
    key on the normalized URL when present, otherwise the normalized headline.
    """
    if art.url:
        return "url:" + art.url.strip().lower().rstrip("/")
    return "head:" + " ".join((art.headline or "").lower().split())


def aggregate_ticker_sentiment(
    ticker: str,
    scored: Sequence[ArticleSentiment],
    *,
    recency_halflife_days: float = 7.0,
    min_articles_for_confidence: int = 3,
    confidence_full_article_count: int = 25,
    confidence_full_source_count: int = 5,
    stale_after_days: float = 14.0,
    model_confidence_factor: float = 0.85,
    model_name: str = "vader",
    now: Optional[datetime] = None,
) -> TickerSentiment:
    """Roll up article-level scores into a single TickerSentiment record.

    sentiment_score = 50 + 50 * recency_weighted_compound, clipped to [0,100].

    Confidence is in [0,1] and is deliberately hard to max out. It blends five
    signals so a ticker can't look "certain" off a thin, stale, duplicated feed:

      * article volume   -- UNIQUE articles vs ``confidence_full_article_count``
      * source diversity -- distinct sources vs ``confidence_full_source_count``
      * freshness        -- share of articles inside ``stale_after_days``
      * de-duplication   -- share of articles that aren't repeats
      * model quality    -- ``model_confidence_factor`` ceiling (lexicon < FinBERT)

    The old model saturated at 3 articles from any source, so 10 near-identical
    yfinance headlines scored 1.0. That is the bug this addresses.
    """
    if not scored:
        return TickerSentiment(ticker=ticker, model_name=model_name, articles=[])

    now = now or datetime.now(timezone.utc)

    # Mark duplicates (first occurrence kept, later repeats flagged) and stale
    # articles, and stamp each article's age in days for downstream reporting.
    seen: set = set()
    weights: List[float] = []
    for art in scored:
        key = _dedup_key(art)
        art.is_duplicate = key in seen
        seen.add(key)

        published_at = _parse_iso(art.published_at) if art.published_at else None
        if published_at is None:
            art.age_days = None
            art.is_stale = False
            weights.append(0.5)  # neutral weight for undated articles
            continue
        age_days = max((now - published_at).total_seconds() / 86400.0, 0.0)
        art.age_days = age_days
        art.is_stale = age_days > stale_after_days
        # half-life decay: w = 0.5 ** (age / halflife)
        decay = math.pow(0.5, age_days / max(recency_halflife_days, 0.01))
        weights.append(decay)

    total_w = sum(weights) or 1e-9
    avg_compound = sum(a.compound for a in scored) / len(scored)
    recency_weighted = sum(a.compound * w for a, w in zip(scored, weights)) / total_w

    pos = sum(1 for a in scored if a.label == "positive")
    neg = sum(1 for a in scored if a.label == "negative")
    neu = sum(1 for a in scored if a.label == "neutral")
    n = len(scored)

    unique = [a for a in scored if not a.is_duplicate]
    unique_n = len(unique)
    duplicate_count = n - unique_n
    stale_count = sum(1 for a in scored if a.is_stale)
    fresh_ratio = (n - stale_count) / n
    unique_ratio = unique_n / n
    # Diversity counts only the sources of NON-duplicate articles.
    sources = {a.source for a in unique if a.source}

    # --- Confidence: a weighted blend, capped by model quality. -------------
    article_factor = min(unique_n / max(confidence_full_article_count, 1), 1.0)
    diversity_factor = min(len(sources) / max(confidence_full_source_count, 1), 1.0)
    raw_confidence = (
        0.40 * article_factor
        + 0.25 * diversity_factor
        + 0.20 * fresh_ratio
        + 0.15 * unique_ratio
    )
    # Below the minimum article floor, damp confidence proportionally so a
    # single article can never look like a trustworthy consensus.
    if unique_n < min_articles_for_confidence:
        raw_confidence *= unique_n / max(min_articles_for_confidence, 1)
    confidence = max(0.0, min(1.0, raw_confidence * model_confidence_factor))

    sentiment_0_100 = max(0.0, min(100.0, 50.0 + 50.0 * recency_weighted))

    return TickerSentiment(
        ticker=ticker,
        sentiment_score=sentiment_0_100,
        average_compound=avg_compound,
        recency_weighted_compound=recency_weighted,
        article_count=n,
        unique_article_count=unique_n,
        duplicate_count=duplicate_count,
        stale_count=stale_count,
        fresh_ratio=fresh_ratio,
        unique_ratio=unique_ratio,
        positive_ratio=pos / n,
        negative_ratio=neg / n,
        neutral_ratio=neu / n,
        source_diversity=len(sources),
        confidence=confidence,
        model_name=model_name,
        articles=list(scored),
    )


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        # Accept trailing 'Z'
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None
