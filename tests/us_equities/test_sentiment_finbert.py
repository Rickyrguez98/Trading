"""FinBERT backend, ensemble, agreement categories, new flags, and the three
new validation checks -- all with mocks, so NOTHING here downloads a model or
imports torch/transformers.

These cover the milestone additions on top of ``test_sentiment_models.py``:

  * the pure device resolver (fake torch, no GPU needed),
  * the batched ``score_many`` contract (probabilities + captured errors),
  * the seven agreement categories,
  * the ensemble blend + ENSEMBLE_SENTIMENT / SENTIMENT_MODEL_FALLBACK /
    FINBERT_SCORING_ERROR flags,
  * per-article FinBERT probabilities flowing through ``score_articles``,
  * the run-summary's new fields,
  * the finbert_availability / sentiment_dominance / sentiment_output_completeness
    validation checks (including the anti-fabrication ``error``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from asset_selection.config import AppConfig, SentimentConfig
from asset_selection.data_providers.base import NewsItem
from asset_selection.sentiment import comparison
from asset_selection.sentiment.comparison import (
    FLAG_ENSEMBLE_SENTIMENT,
    FLAG_FINBERT_SCORING_ERROR,
    FLAG_SENTIMENT_MODEL_FALLBACK,
    SentimentRuntime,
    build_run_sentiment_summary,
    build_sentiment_runtime,
    resolve_ticker_sentiment,
)
from asset_selection.sentiment.sentiment_model import (
    ScoreResult,
    SentimentModel,
    VaderSentimentModel,
    _resolve_finbert_device,
    score_articles,
)
from asset_selection.validation import validate_outputs


# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------

class DummyModel(SentimentModel):
    """Deterministic compound-only stub (uses the default ``score_many``)."""

    def __init__(self, scores):
        self._scores = list(scores)
        self._idx = 0

    def score(self, text: str) -> float:  # noqa: ARG002
        v = self._scores[self._idx % len(self._scores)]
        self._idx += 1
        return v


class ProbFinbert(SentimentModel):
    """A 'FinBERT' that emits class probabilities via ``score_many``."""

    def __init__(self, compound=0.4, pos=0.6, neu=0.3, neg=0.1):
        self._c, self._p, self._u, self._n = compound, pos, neu, neg

    def score(self, text: str) -> float:  # noqa: ARG002
        return self._c

    def score_many(self, texts):
        return [
            ScoreResult(
                compound=self._c,
                positive_probability=self._p,
                neutral_probability=self._u,
                negative_probability=self._n,
            )
            for _ in texts
        ]


def _fresh_articles(n: int):
    base = datetime.now(timezone.utc)
    return [
        NewsItem(
            ticker="ZZZ",
            headline=f"Headline {i}",
            summary=f"Body {i}",
            source=f"Wire{i % 3}",
            url=f"https://example.com/{i}",
            published_at=(base - timedelta(hours=i)).isoformat(),
            retrieved_at=base.isoformat(),
        )
        for i in range(n)
    ]


def _injected_runtime(
    vader, finbert, *, comparison_mode=True, model_name="comparison",
    final_sentiment_source="vader", finbert_attempted=True,
):
    return SentimentRuntime(
        vader_model=vader,
        finbert_model=finbert,
        comparison_mode=comparison_mode,
        finbert_deps_present=finbert is not None,
        finbert_loaded=finbert is not None,
        finbert_attempted=finbert_attempted,
        model_name=model_name,
        final_sentiment_source=final_sentiment_source,
        finbert_model_name="ProsusAI/finbert",
        finbert_device_used="cpu" if finbert is not None else None,
    )


# ---------------------------------------------------------------------------
# Fake torch for the device resolver (no real GPU / no torch import).
# ---------------------------------------------------------------------------

class _FakeAvail:
    def __init__(self, avail: bool):
        self._a = avail

    def is_available(self) -> bool:
        return self._a


class _FakeBackends:
    def __init__(self, mps: bool):
        self.mps = _FakeAvail(mps)


class FakeTorch:
    def __init__(self, cuda: bool = False, mps: bool = False):
        self.cuda = _FakeAvail(cuda)
        self.backends = _FakeBackends(mps)


def test_device_auto_prefers_cuda_then_mps_then_cpu():
    assert _resolve_finbert_device(FakeTorch(cuda=True, mps=True), "auto") == "cuda"
    assert _resolve_finbert_device(FakeTorch(cuda=False, mps=True), "auto") == "mps"
    assert _resolve_finbert_device(FakeTorch(cuda=False, mps=False), "auto") == "cpu"


def test_device_explicit_falls_back_when_unavailable():
    assert _resolve_finbert_device(FakeTorch(cuda=False), "cuda") == "cpu"
    assert _resolve_finbert_device(FakeTorch(mps=False), "mps") == "cpu"
    assert _resolve_finbert_device(FakeTorch(cuda=True), "cpu") == "cpu"


# ---------------------------------------------------------------------------
# score_many contract
# ---------------------------------------------------------------------------

def test_score_many_default_wraps_score():
    res = DummyModel([0.5, -0.25]).score_many(["a", "b"])
    assert [r.compound for r in res] == [0.5, -0.25]
    # The lexicon default carries no class probabilities.
    assert res[0].positive_probability is None
    assert all(r.error is None for r in res)


def test_score_many_captures_errors_without_raising():
    class Boom(SentimentModel):
        def score(self, text: str) -> float:  # noqa: ARG002
            raise RuntimeError("kaboom")

    res = Boom().score_many(["x", "y"])
    assert all(r.compound == 0.0 for r in res)
    assert all(r.error and "RuntimeError" in r.error for r in res)


def test_score_articles_carries_finbert_probabilities():
    scored = score_articles(_fresh_articles(2), ProbFinbert(pos=0.7, neu=0.2, neg=0.1))
    assert scored[0].finbert_positive_probability == 0.7
    assert scored[0].finbert_neutral_probability == 0.2
    assert scored[0].finbert_negative_probability == 0.1
    assert scored[0].scoring_error is None


# ---------------------------------------------------------------------------
# Agreement categories (seven)
# ---------------------------------------------------------------------------

def test_agreement_mild_disagreement():
    cfg = SentimentConfig(model="comparison", model_disagreement_threshold=20.0)
    # vader -> 50, finbert -> 62 -> delta 12 in [10, 20) -> mild.
    rt = _injected_runtime(DummyModel([0.0]), DummyModel([0.24]))
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert row["sentiment_model_agreement"] == "mild_disagreement"
    assert "SENTIMENT_MODEL_DISAGREEMENT" not in row["sentiment_flags"]


def test_agreement_negative_and_neutral():
    cfg = SentimentConfig(model="comparison", model_disagreement_threshold=20.0)
    neg = resolve_ticker_sentiment(
        "ZZZ", _fresh_articles(3),
        _injected_runtime(DummyModel([-0.5]), DummyModel([-0.5])), cfg,
    )
    assert neg["sentiment_model_agreement"] == "agreement_negative"

    neu = resolve_ticker_sentiment(
        "ZZZ", _fresh_articles(3),
        _injected_runtime(DummyModel([0.0]), DummyModel([0.0])), cfg,
    )
    assert neu["sentiment_model_agreement"] == "agreement_neutral"


def test_agreement_finbert_unavailable_vs_vader_only(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)

    # Comparison wanted FinBERT but it is unavailable -> finbert_unavailable.
    cfg_cmp = SentimentConfig(model="comparison")
    rt_cmp = build_sentiment_runtime(cfg_cmp)
    row_cmp = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt_cmp, cfg_cmp)
    assert row_cmp["sentiment_model_agreement"] == "finbert_unavailable"

    # Plain VADER never asked for FinBERT -> vader_only.
    cfg_v = SentimentConfig(model="vader")
    rt_v = build_sentiment_runtime(cfg_v)
    row_v = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt_v, cfg_v)
    assert row_v["sentiment_model_agreement"] == "vader_only"


# ---------------------------------------------------------------------------
# Ensemble + new flags
# ---------------------------------------------------------------------------

def test_ensemble_runtime_forces_blended_source(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    rt = build_sentiment_runtime(SentimentConfig(model="ensemble"))
    assert rt.comparison_mode is True
    assert rt.final_sentiment_source == "ensemble"
    assert rt.finbert_attempted is True


def test_ensemble_sets_flag_and_blends():
    cfg = SentimentConfig(
        model="ensemble", ensemble_vader_weight=0.4, ensemble_finbert_weight=0.6,
    )
    rt = _injected_runtime(
        DummyModel([1.0]), DummyModel([-1.0]),
        model_name="ensemble", final_sentiment_source="ensemble",
    )
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert row["sentiment_model_used"] == "ensemble"
    assert FLAG_ENSEMBLE_SENTIMENT in row["sentiment_flags"]
    # vader 100, finbert 0 -> 0.4*100 + 0.6*0 = 40.
    assert abs(row["sentiment_score"] - 40.0) < 1e-6


def test_single_finbert_fallback_sets_fallback_flag(monkeypatch):
    monkeypatch.setattr(comparison, "is_finbert_available", lambda: False)
    cfg = SentimentConfig(model="finbert", fallback_to_vader_if_finbert_unavailable=True)
    rt = build_sentiment_runtime(cfg)
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert FLAG_SENTIMENT_MODEL_FALLBACK in row["sentiment_flags"]
    assert row["sentiment_model_fallback_used"] is True
    assert row["sentiment_model_used"] == "vader"


def test_finbert_scoring_error_flag():
    class ErrFinbert(SentimentModel):
        def score(self, text: str) -> float:  # noqa: ARG002
            return 0.0

        def score_many(self, texts):
            out = []
            for i, _ in enumerate(texts):
                if i == 0:
                    out.append(ScoreResult(compound=0.0, error="RuntimeError: boom"))
                else:
                    out.append(ScoreResult(compound=0.3, positive_probability=0.5,
                                           neutral_probability=0.3, negative_probability=0.2))
            return out

    cfg = SentimentConfig(model="comparison")
    rt = _injected_runtime(VaderSentimentModel(), ErrFinbert())
    row = resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)
    assert FLAG_FINBERT_SCORING_ERROR in row["sentiment_flags"]
    assert row["finbert_scoring_error_count"] == 1


# ---------------------------------------------------------------------------
# Run summary new fields
# ---------------------------------------------------------------------------

def test_run_summary_reports_new_fields():
    cfg = SentimentConfig(model="comparison", final_sentiment_source="vader",
                          model_disagreement_threshold=10.0)
    rt = _injected_runtime(DummyModel([0.0]), DummyModel([-1.0]))
    rows = [resolve_ticker_sentiment("ZZZ", _fresh_articles(3), rt, cfg)]
    summary = build_run_sentiment_summary(rt, rows)
    assert summary["final_sentiment_source"] == "vader"
    assert summary["finbert_model_name"] == "ProsusAI/finbert"
    assert summary["finbert_device_used"] == "cpu"
    assert "agreement_breakdown" in summary
    assert summary["agreement_breakdown"]["strong_disagreement"] == 1
    assert summary["sentiment_model_disagreement_count"] == 1


# ---------------------------------------------------------------------------
# Validation: the three new checks
# ---------------------------------------------------------------------------

def _ranked(**overrides):
    row = {
        "ticker": "AAA", "company_name": "Alpha Inc", "market_cap": 1e11,
        "market_cap_available": True, "volatility_pct": 0.3, "return_pct": 0.1,
        "flags": [], "selection_bucket": "high_quality_core_candidate",
        "article_count": 6, "unique_article_count": 6, "stale_count": 0,
        "fresh_ratio": 1.0, "source_diversity": 4, "sentiment_confidence": 0.5,
        "sentiment_score": 60.0, "vader_sentiment_score": 60.0,
        "sentiment_model_used": "vader", "sentiment_model_agreement": "vader_only",
        "final_sentiment_score": 60.0,
        "growth_score": 60.0, "quality_score": 62.0, "valuation_score": 58.0,
        "balance_sheet_score": 59.0, "cash_flow_score": 61.0,
    }
    row.update(overrides)
    return row


def _summary(**ss):
    base = {
        "configured_model": "comparison", "sentiment_model_used": "vader",
        "finbert_available": False, "articles_scored_vader": 10,
        "articles_scored_finbert": 0, "final_sentiment_source": "vader",
        "finbert_attempted": True,
    }
    base.update(ss)
    return {"provider_failures": {"total": 0}, "sentiment_summary": base}


def test_validation_finbert_fabrication_is_error():
    # A FinBERT score present while FinBERT is unavailable & scored nothing.
    ranked = pd.DataFrame([_ranked(finbert_sentiment_score=42.0)])
    report = validate_outputs(ranked, _summary(), AppConfig())
    check = next(c for c in report["checks"] if c["name"] == "finbert_availability")
    assert check["status"] == "error"
    assert report["overall_status"] == "error"


def test_validation_finbert_unavailable_is_warn_not_error():
    ranked = pd.DataFrame([_ranked()])  # no finbert score present
    report = validate_outputs(ranked, _summary(), AppConfig())
    check = next(c for c in report["checks"] if c["name"] == "finbert_availability")
    assert check["status"] == "warn"


def test_validation_sentiment_dominance_error_when_overweighted():
    cfg = AppConfig()
    cfg.composite.weights = {
        "fundamentals": 0.20, "growth": 0.10, "quality": 0.10,
        "valuation": 0.10, "sentiment": 0.40, "risk": 0.20,
    }
    report = validate_outputs(pd.DataFrame([_ranked()]), _summary(), cfg)
    check = next(c for c in report["checks"] if c["name"] == "sentiment_dominance")
    assert check["status"] == "error"


def test_validation_sentiment_dominance_ok_under_default_weights():
    cfg = AppConfig()
    cfg.composite.weights = {
        "fundamentals": 0.50, "growth": 0.15, "quality": 0.10,
        "valuation": 0.10, "sentiment": 0.15, "risk": 0.20,
    }
    report = validate_outputs(pd.DataFrame([_ranked()]), _summary(), cfg)
    check = next(c for c in report["checks"] if c["name"] == "sentiment_dominance")
    assert check["status"] == "ok"


def test_validation_output_completeness_warns_on_missing_summary_keys():
    ss = _summary()
    del ss["sentiment_summary"]["articles_scored_finbert"]
    report = validate_outputs(pd.DataFrame([_ranked()]), ss, AppConfig())
    check = next(c for c in report["checks"] if c["name"] == "sentiment_output_completeness")
    assert check["status"] == "warn"
