"""VADER vs FinBERT model interface, comparison, and safe-fallback behavior.

These tests must pass WITHOUT the optional ``[finbert]`` extras installed:
FinBERT is mocked or reported unavailable. They prove the four guarantees:

  * VADER always works with no extra deps.
  * A requested-but-unavailable FinBERT never crashes and is reported, not faked.
  * Comparison mode flags large model disagreements.
  * The confidence-adjusted effective sentiment still keeps fundamentals on top.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from asset_selection.config import SentimentConfig, load_config
from asset_selection.data_providers.base import NewsItem
from asset_selection.scoring.composite_score import (
    compute_composite_scores,
    compute_effective_sentiment,
)
from asset_selection.sentiment import comparison
from asset_selection.sentiment.comparison import (
    SentimentRuntime,
    _attach_dual_scores,
    build_run_sentiment_summary,
    build_sentiment_runtime,
    resolve_ticker_sentiment,
)
from asset_selection.sentiment.sentiment_model import (
    SentimentModel,
    VaderSentimentModel,
    score_articles,
)


class DummyModel(SentimentModel):
    """Deterministic stub so a 'FinBERT' can be injected without real deps."""

    def __init__(self, scores):
        self._scores = list(scores)
        self._idx = 0

    def score(self, text: str) -> float:  # noqa: ARG002
        v = self._scores[self._idx % len(self._scores)]
        self._idx += 1
        return v


def _fresh_articles(n: int, source_prefix: str = "Wire"):
    """n distinct, fresh, dated articles (so recency weighting is well-defined)."""
    base = datetime.now(timezone.utc)
    return [
        NewsItem(
            ticker="ZZZ",
            headline=f"Headline {i}",
            summary=f"Body {i}",
            source=f"{source_prefix}{i % 3}",
            url=f"https://example.com/{i}",
            published_at=(base - timedelta(hours=i)).isoformat(),
            retrieved_at=base.isoformat(),
        )
        for i in range(n)
    ]


def _injected_runtime(vader, finbert, *, comparison_mode=True, model_name="comparison"):
    """Build a runtime with a mocked FinBERT, bypassing the import probe."""
    return SentimentRuntime(
        vader_model=vader,
        finbert_model=finbert,
        comparison_mode=comparison_mode,
        finbert_deps_present=finbert is not None,
        finbert_loaded=finbert is not None,
        finbert_attempted=True,
        model_name=model_name,
    )


# ---------------------------------------------------------------------------
# 1. VADER works with no FinBERT deps.
# ---------------------------------------------------------------------------

def test_vader_runtime_runs_without_finbert(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    cfg = SentimentConfig(model="vader")
    rt = build_sentiment_runtime(cfg)
    assert rt.finbert_usable is False
    assert rt.finbert_model is None

    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert row["sentiment_model_used"] == "vader"
    assert row["finbert_sentiment_score"] is None
    assert row["vader_sentiment_score"] is not None
    assert 0.0 <= row["sentiment_score"] <= 100.0


# ---------------------------------------------------------------------------
# 2. FinBERT requested but unavailable -> safe fallback, no crash, reported.
# ---------------------------------------------------------------------------

def test_finbert_unavailable_falls_back_to_vader(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    cfg = SentimentConfig(model="finbert", fallback_to_vader_if_finbert_unavailable=True)
    rt = build_sentiment_runtime(cfg)
    assert rt.finbert_attempted is True
    assert rt.finbert_usable is False
    assert rt.finbert_unavailable_reason  # an honest reason is recorded

    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert row["sentiment_model_used"] == "vader"
    assert "FINBERT_UNAVAILABLE" in row["sentiment_flags"]
    assert "VADER_ONLY_SENTIMENT" in row["sentiment_flags"]
    # The fallback used VADER's real score -- nothing fabricated.
    assert row["sentiment_score"] == row["vader_sentiment_score"]


def test_finbert_unavailable_without_fallback_is_neutral_not_faked(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    cfg = SentimentConfig(model="finbert", fallback_to_vader_if_finbert_unavailable=False)
    rt = build_sentiment_runtime(cfg)
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert "FINBERT_UNAVAILABLE" in row["sentiment_flags"]
    assert row["sentiment_model_used"] == "none"
    assert row["sentiment_score"] == cfg.neutral_sentiment_score
    assert row["finbert_sentiment_score"] is None


# ---------------------------------------------------------------------------
# 3. Comparison mode reports FINBERT_UNAVAILABLE when deps missing.
# ---------------------------------------------------------------------------

def test_comparison_mode_reports_finbert_unavailable(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    cfg = SentimentConfig(model="vader", compare_models=True)
    rt = build_sentiment_runtime(cfg)
    assert rt.comparison_mode is True

    rows = [resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)]
    summary = build_run_sentiment_summary(rt, rows)
    assert summary["comparison_mode"] is True
    assert summary["finbert_available"] is False
    assert "FINBERT_UNAVAILABLE" in summary["notes"]
    assert "FINBERT_UNAVAILABLE" in rows[0]["sentiment_flags"]


# ---------------------------------------------------------------------------
# 4. Mock FinBERT scores are handled and can drive the final score.
# ---------------------------------------------------------------------------

def test_mock_finbert_comparison_finbert_final():
    cfg = SentimentConfig(model="comparison", final_sentiment_source="finbert")
    rt = _injected_runtime(VaderSentimentModel(), DummyModel([0.6, 0.6, 0.6]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)

    assert row["finbert_sentiment_score"] is not None
    assert row["vader_sentiment_score"] is not None
    assert row["sentiment_model_used"] == "finbert"
    assert row["sentiment_score"] == row["finbert_sentiment_score"]
    # FinBERT scored each of the 3 articles.
    assert row["_finbert_articles_scored"] == 3


def test_ensemble_blends_both_models():
    cfg = SentimentConfig(
        model="comparison", final_sentiment_source="ensemble",
        ensemble_vader_weight=0.5, ensemble_finbert_weight=0.5,
    )
    rt = _injected_runtime(DummyModel([1.0]), DummyModel([-1.0]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    # vader -> 100, finbert -> 0, 50/50 blend -> ~50.
    assert row["sentiment_model_used"] == "ensemble"
    assert abs(row["sentiment_score"] - 50.0) < 1e-6


# ---------------------------------------------------------------------------
# 5. Large disagreement creates a flag.
# ---------------------------------------------------------------------------

def test_large_disagreement_sets_flag():
    cfg = SentimentConfig(model="comparison", sentiment_disagreement_threshold=10.0)
    rt = _injected_runtime(DummyModel([0.0]), DummyModel([-1.0]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    # vader -> 50, finbert -> 0 -> delta 50 >> 10 -> strong disagreement.
    assert row["sentiment_model_agreement"] == "strong_disagreement"
    assert "SENTIMENT_MODEL_DISAGREEMENT" in row["sentiment_flags"]
    assert row["sentiment_score_delta"] is not None and row["sentiment_score_delta"] > 10.0


def test_agreement_does_not_flag():
    cfg = SentimentConfig(model="comparison", sentiment_disagreement_threshold=25.0)
    rt = _injected_runtime(DummyModel([0.5]), DummyModel([0.5]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    # Both models -> 75 (positive), zero delta -> agreement, positive direction.
    assert row["sentiment_model_agreement"] == "agreement_positive"
    assert "SENTIMENT_MODEL_DISAGREEMENT" not in row["sentiment_flags"]


# ---------------------------------------------------------------------------
# 6. LOW_FINBERT_CONFIDENCE flag fires on a low-magnitude FinBERT feed.
# ---------------------------------------------------------------------------

def test_low_finbert_confidence_flag():
    cfg = SentimentConfig(model="comparison", low_finbert_confidence_threshold=0.5)
    rt = _injected_runtime(DummyModel([0.0]), DummyModel([0.1, -0.1]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(4), rt, cfg)
    # mean |finbert| = 0.1 < 0.5.
    assert "LOW_FINBERT_CONFIDENCE" in row["sentiment_flags"]


# ---------------------------------------------------------------------------
# 7. Per-article output carries BOTH models' score + label.
# ---------------------------------------------------------------------------

def test_per_article_dual_scores_attached():
    arts = _fresh_articles(2)
    v = score_articles(arts, DummyModel([0.8, 0.8]))
    f = score_articles(arts, DummyModel([-0.6, -0.6]))
    _attach_dual_scores(v, f)
    assert v[0].vader_score == 0.8
    assert v[0].finbert_score == -0.6
    assert v[0].vader_label == "positive"
    assert v[0].finbert_label == "negative"
    assert v[0].model_used == "vader"
    assert f[0].model_used == "finbert"


# ---------------------------------------------------------------------------
# 8. Effective sentiment keeps confidence adjustment; fundamentals dominate.
# ---------------------------------------------------------------------------

def test_effective_sentiment_pulls_low_confidence_toward_neutral():
    cfg = SentimentConfig(use_confidence_adjusted_sentiment=True, neutral_sentiment_score=50.0)
    df = pd.DataFrame({
        "sentiment_score": [80.0, 80.0],
        "sentiment_confidence": [1.0, 0.0],
    })
    eff = compute_effective_sentiment(df, cfg)
    assert abs(eff.iloc[0] - 80.0) < 1e-6   # full confidence -> unchanged
    assert abs(eff.iloc[1] - 50.0) < 1e-6   # zero confidence -> neutral


def test_fundamentals_dominate_sentiment_under_default_weights():
    cfg = load_config(None)
    w = cfg.composite.weights
    assert w["fundamentals"] > w["sentiment"]

    base = {
        "fundamentals_score": 50, "growth_score": 50, "quality_score": 50,
        "valuation_score": 50, "sentiment_score": 50, "risk_penalty": 0,
    }
    c0 = compute_composite_scores(pd.DataFrame([base]), cfg.composite).iloc[0]
    c_sent = compute_composite_scores(
        pd.DataFrame([{**base, "sentiment_score": 100}]), cfg.composite
    ).iloc[0]
    c_fund = compute_composite_scores(
        pd.DataFrame([{**base, "fundamentals_score": 100}]), cfg.composite
    ).iloc[0]
    # Moving fundamentals 50->100 must move the composite more than moving
    # sentiment 50->100 does.
    assert (c_fund - c0) > (c_sent - c0)
