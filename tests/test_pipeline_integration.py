"""End-to-end pipeline smoke test with mocked providers.

This test exercises the full ``run_asset_selection.main`` path on a tiny
list of tickers, but stubs out every external provider so the test is
hermetic (no network, no yfinance dependency at test time).

It guards against the regressions that would otherwise only surface during
a live run:
  * pipeline crashes when news returns []
  * CSV / JSON / Markdown files are produced
  * top candidates have the required schema fields populated
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from asset_selection.data_providers.base import (
    Fundamentals,
    NewsItem,
    PriceSnapshot,
)


def _mock_fundamentals(ticker: str) -> Fundamentals:
    profiles = {
        "AAPL": Fundamentals(
            ticker="AAPL", company_name="Apple Inc.", sector="Technology",
            industry="Consumer Electronics", market_cap=3e12,
            revenue_growth=0.08, earnings_growth=0.10, fcf_growth=0.07,
            operating_margin=0.30, net_margin=0.25, roe=1.5, roa=0.30,
            debt_to_equity=150.0, current_ratio=1.1,
            free_cash_flow=1e11, operating_cash_flow=1.2e11,
            free_cash_flow_yield=0.04, operating_cash_flow_margin=0.30,
            pe_ratio=30.0, forward_pe=28.0, peg_ratio=2.8,
            price_to_sales=7.0, price_to_book=45.0, source="mock",
        ),
        "MSFT": Fundamentals(
            ticker="MSFT", company_name="Microsoft Corp.", sector="Technology",
            industry="Software", market_cap=2.8e12,
            revenue_growth=0.14, earnings_growth=0.17, fcf_growth=0.12,
            operating_margin=0.42, net_margin=0.36, roe=0.40, roa=0.20,
            debt_to_equity=40.0, current_ratio=1.8,
            free_cash_flow=7e10, operating_cash_flow=9e10,
            free_cash_flow_yield=0.025, operating_cash_flow_margin=0.38,
            pe_ratio=33.0, forward_pe=30.0, peg_ratio=2.5,
            price_to_sales=11.0, price_to_book=12.0, source="mock",
        ),
        "GOOGL": Fundamentals(
            ticker="GOOGL", company_name="Alphabet Inc.", sector="Technology",
            industry="Internet Content", market_cap=2.0e12,
            revenue_growth=0.11, earnings_growth=0.15, fcf_growth=0.09,
            operating_margin=0.30, net_margin=0.24, roe=0.28, roa=0.20,
            debt_to_equity=10.0, current_ratio=2.1,
            free_cash_flow=6e10, operating_cash_flow=9e10,
            free_cash_flow_yield=0.03, operating_cash_flow_margin=0.32,
            pe_ratio=27.0, forward_pe=24.0, peg_ratio=1.7,
            price_to_sales=6.5, price_to_book=7.0, source="mock",
        ),
    }
    # Unknown tickers get a generic-but-complete profile (real market cap +
    # metrics). This keeps synthetic-universe tests above the coverage gate's
    # "valid candidates need real fundamentals" floor; an all-empty fallback
    # would (correctly) be judged INSUFFICIENT_DATA and exit 2.
    generic = Fundamentals(
        ticker=ticker, company_name=f"{ticker} Inc.", sector="Industrials",
        industry="Diversified", market_cap=5e10,
        revenue_growth=0.08, earnings_growth=0.09, fcf_growth=0.07,
        operating_margin=0.18, net_margin=0.12, roe=0.18, roa=0.10,
        debt_to_equity=70.0, current_ratio=1.6,
        free_cash_flow=3e9, operating_cash_flow=4e9,
        free_cash_flow_yield=0.03, operating_cash_flow_margin=0.16,
        pe_ratio=20.0, forward_pe=18.0, peg_ratio=1.6,
        price_to_sales=4.0, price_to_book=3.0, source="mock",
    )
    profile = profiles.get(ticker, generic)
    # Tracked-missing-fields logic in the real provider is replicated here
    # so the test reflects production behaviour.
    tracked = [
        "market_cap", "revenue_growth", "earnings_growth", "operating_margin",
        "net_margin", "roe", "roa", "debt_to_equity", "current_ratio",
        "free_cash_flow", "operating_cash_flow", "pe_ratio", "forward_pe",
        "peg_ratio", "price_to_sales", "price_to_book",
    ]
    profile.missing_fields = [f for f in tracked if getattr(profile, f) is None]
    return profile


def _mock_prices(ticker: str, lookback_days: int = 90) -> PriceSnapshot:
    profiles = {
        "AAPL": (200.0, 50_000_000, 0.18, 0.25),
        "MSFT": (420.0, 25_000_000, 0.22, 0.22),
        "GOOGL": (170.0, 35_000_000, -0.12, 0.30),  # negative -> WEAK_PRICE_TREND
    }
    # Synthetic valid defaults for unknown tickers so they still pass the
    # liquidity floor and volatility-history check.
    last, vol, ret, sigma = profiles.get(ticker, (100.0, 5_000_000, 0.10, 0.25))
    return PriceSnapshot(
        ticker=ticker,
        last_close=last,
        avg_daily_volume=vol,
        avg_dollar_volume=(vol * last) if vol and last else None,
        return_pct=ret,
        volatility_pct=sigma,
        lookback_days=lookback_days,
        source="mock",
    )


def _mock_news(ticker: str, max_age_days: int = 30) -> List[NewsItem]:
    # AAPL: two upbeat items, MSFT: empty list, GOOGL: one mildly negative.
    if ticker == "AAPL":
        return [
            NewsItem(ticker=ticker, headline="Apple reports record revenue and strong outlook",
                    summary="Profits surge to all-time highs", source="WireA",
                    url="https://example.com/a1", published_at="2026-06-01T12:00:00+00:00",
                    retrieved_at="2026-06-05T12:00:00+00:00"),
            NewsItem(ticker=ticker, headline="Apple beats expectations on iPhone demand",
                    summary="Bullish guidance from management", source="WireB",
                    url="https://example.com/a2", published_at="2026-06-03T12:00:00+00:00",
                    retrieved_at="2026-06-05T12:00:00+00:00"),
        ]
    if ticker == "GOOGL":
        return [
            NewsItem(ticker=ticker, headline="Alphabet faces regulatory pressure and weak ad market",
                    summary="Concerns over slowing growth", source="WireC",
                    url="https://example.com/g1", published_at="2026-06-04T12:00:00+00:00",
                    retrieved_at="2026-06-05T12:00:00+00:00"),
        ]
    return []  # MSFT -> empty (tests the no-news path)


class _PatchedFundamentals:
    def __init__(self, cache=None, rate_limiter=None):
        pass
    def fetch(self, ticker: str) -> Fundamentals:
        return _mock_fundamentals(ticker)


class _PatchedPrices:
    def __init__(self, cache=None, rate_limiter=None):
        pass
    def fetch(self, ticker: str, lookback_days: int = 90) -> PriceSnapshot:
        return _mock_prices(ticker, lookback_days)


class _PatchedNews:
    def __init__(self, cache=None, rate_limiter=None):
        pass
    def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
        return _mock_news(ticker, max_age_days)


@pytest.fixture
def temp_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The pipeline writes to relative paths under cwd. Move there.
    monkeypatch.chdir(tmp_path)
    # Copy the default config so the pipeline can load it from the tmp cwd.
    src_cfg = Path(__file__).resolve().parent.parent / "configs" / "default_config.yaml"
    (tmp_path / "configs").mkdir()
    shutil.copy(src_cfg, tmp_path / "configs" / "default_config.yaml")
    return tmp_path


def test_pipeline_runs_end_to_end_with_mocked_providers(temp_workdir: Path):
    from asset_selection.pipelines import run_asset_selection as r

    # Patch only the provider classes; everything else is real.
    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "MSFT", "GOOGL",
            "--top", "3",
            "--no-cache",
            "--log-level", "ERROR",
        ])

    assert rc == 0, "Pipeline must exit 0 on the happy path."

    csv = temp_workdir / "data" / "processed" / "asset_selection_results.csv"
    md = temp_workdir / "reports" / "top_candidates.md"
    js = temp_workdir / "reports" / "asset_selection_summary.json"
    assert csv.exists() and csv.stat().st_size > 0, "CSV must be written and non-empty."
    assert md.exists() and md.stat().st_size > 0, "Markdown must be written and non-empty."
    assert js.exists() and js.stat().st_size > 0, "JSON summary must be written."

    summary = json.loads(js.read_text())
    tickers = {c["ticker"] for c in summary["candidates"]}
    assert tickers == {"AAPL", "MSFT", "GOOGL"}, f"Unexpected tickers: {tickers}"

    by_ticker = {c["ticker"]: c for c in summary["candidates"]}

    # MSFT had zero news -> NO_NEWS flag, pipeline did not crash.
    assert "NO_NEWS" in by_ticker["MSFT"]["warning_flags"]
    assert by_ticker["MSFT"]["sentiment_article_count"] == 0

    # GOOGL had negative return -> WEAK_PRICE_TREND flag.
    assert "WEAK_PRICE_TREND" in by_ticker["GOOGL"]["warning_flags"]

    # Every candidate must carry the required-for-spec fields.
    required = {
        "ticker", "company_name", "sector", "industry",
        "market_cap", "avg_dollar_volume",
        "sentiment_score", "sentiment_article_count",
        "fundamentals_score", "growth_score", "quality_score", "valuation_score",
        "risk_penalty", "final_score", "rank",
        "reason", "warning_flags", "missing_fields",
    }
    for c in summary["candidates"]:
        missing = required - set(c.keys())
        assert not missing, f"{c['ticker']} missing fields: {missing}"
        assert c["reason"], f"{c['ticker']} has empty reason"
        assert isinstance(c["warning_flags"], list)


def test_pipeline_does_not_crash_when_all_news_empty(temp_workdir: Path):
    """Even with zero articles for every ticker, the pipeline still produces
    a complete, valid report."""
    from asset_selection.pipelines import run_asset_selection as r

    class _EmptyNews:
        def __init__(self, cache=None, rate_limiter=None):
            pass
        def fetch(self, ticker: str, max_age_days: int = 30) -> List[NewsItem]:
            return []

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_EmptyNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "MSFT",
            "--top", "2",
            "--no-cache",
            "--log-level", "ERROR",
        ])

    assert rc == 0
    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    for c in js["candidates"]:
        assert "NO_NEWS" in c["warning_flags"]
        assert c["sentiment_article_count"] == 0
        # Default fill: 50 (neutral) when no news is available.
        assert c["sentiment_score"] == 50.0


# ---------------------------------------------------------------------------
# Staged-pipeline behaviour
# ---------------------------------------------------------------------------

def test_summary_includes_stage_stats_and_exchange_breakdown(temp_workdir: Path):
    """The new staged pipeline must surface per-stage in/out counts and an
    exchange breakdown in the JSON summary, plus a universe_summary.json."""
    from asset_selection.pipelines import run_asset_selection as r

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "MSFT", "GOOGL",
            "--top", "3", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0

    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    assert js["mode"] == "custom"
    stage_names = [s["name"] for s in js["stages"]]
    assert stage_names == [
        "1_universe", "2_prices", "3_fundamentals",
        "4_sentiment", "5_compose_and_rank",
    ]
    # Each stage carries an output_count and a dropped dict.
    for s in js["stages"]:
        assert "output_count" in s
        assert "dropped" in s
        assert "duration_seconds" in s

    uni_summary = json.loads(
        (temp_workdir / "reports" / "universe_summary.json").read_text()
    )
    assert uni_summary["mode"] == "custom"
    assert [s["name"] for s in uni_summary["stages"]] == stage_names


def test_news_runs_only_after_fundamentals_prescreen(temp_workdir: Path):
    """Stage 4 (news/sentiment) must NOT be called for tickers dropped in
    stage 3 (fundamentals)."""
    from asset_selection.pipelines import run_asset_selection as r

    # One ticker has a tiny market cap and will be dropped at stage 3.
    class _BigCapFund:
        def __init__(self, **k): pass
        def fetch(self, ticker):
            tiny = ticker == "TINY"
            return Fundamentals(
                ticker=ticker, company_name=f"{ticker} Co",
                market_cap=(1e6 if tiny else 5e10), sector="Tech",
                revenue_growth=0.1, earnings_growth=0.1, fcf_growth=0.1,
                roe=0.2, roa=0.1, operating_margin=0.2, net_margin=0.1,
                debt_to_equity=80.0, current_ratio=1.5,
                free_cash_flow_yield=0.03, operating_cash_flow_margin=0.15,
                pe_ratio=20.0, forward_pe=18.0, peg_ratio=1.5,
                price_to_sales=5.0, price_to_book=6.0, source="mock",
            )

    news_calls: List[str] = []

    class _SpyNews:
        def __init__(self, **k): pass
        def fetch(self, ticker, max_age_days=30):
            news_calls.append(ticker)
            return []

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_BigCapFund), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_SpyNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "TINY", "MSFT",
            "--top", "3", "--no-cache", "--log-level", "ERROR",
            # Isolate stage-4 gating: the pre-run health check legitimately
            # probes benchmark tickers and would otherwise pollute the spy.
            "--no-provider-health-check",
        ])
    assert rc == 0

    # TINY was dropped at stage 3 -> news must NOT have been called for it.
    assert "TINY" not in news_calls, (
        "Stage 4 was called for a ticker that should have been dropped at "
        "stage 3, defeating the free-API protection."
    )
    assert set(news_calls) == {"AAPL", "MSFT"}


def test_sample_mode_respects_limit(temp_workdir: Path):
    """In sample mode, --limit must actually cap the stage-1 universe."""
    from asset_selection.pipelines import run_asset_selection as r

    # Use a custom universe trick: monkeypatch build_universe to return a
    # synthetic 10-row universe so we can prove --limit reduces it to 3.
    import pandas as pd

    def _fake_build_universe(*_args, **_kwargs):
        return pd.DataFrame({
            "ticker": [f"T{i}" for i in range(10)],
            "company_name": [f"T{i} Co" for i in range(10)],
            "exchange": ["NASDAQ"] * 10,
            "asset_type": ["common"] * 10,
            "is_etf": [False] * 10,
            "is_test_issue": [False] * 10,
            "source": ["nasdaq_trader"] * 10,
        })

    with patch.object(r, "build_universe", side_effect=_fake_build_universe), \
         patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--universe", "sample", "--limit", "3",
            "--top", "3", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0

    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    assert js["mode"] == "sample"
    assert js["sample_limit"] == 3
    s1 = js["stages"][0]
    assert s1["name"] == "1_universe"
    assert s1["output_count"] == 3, (
        f"Sample limit should cap to 3, got {s1['output_count']}"
    )


def test_full_mode_does_not_silently_cap_at_50(temp_workdir: Path):
    """The headline regression: --universe full must NOT cap at 50/500.
    Stage 1 should keep the entire cleaned universe."""
    from asset_selection.pipelines import run_asset_selection as r
    import pandas as pd

    def _big_universe(*_a, **_k):
        return pd.DataFrame({
            "ticker": [f"T{i:04d}" for i in range(800)],
            "company_name": [f"T{i:04d} Co" for i in range(800)],
            "exchange": ["NASDAQ"] * 800,
            "asset_type": ["common"] * 800,
            "is_etf": [False] * 800,
            "is_test_issue": [False] * 800,
            "source": ["nasdaq_trader"] * 800,
        })

    # Use prices/fundamentals that return None / NaN volatility so stage 2
    # drops them via 'insufficient_price_history' -- we only care that
    # stage 1 itself doesn't cap.
    class _NoOpPrices:
        def __init__(self, **k): pass
        def fetch(self, ticker, lookback_days=90):
            return PriceSnapshot(
                ticker=ticker, last_close=100.0,
                avg_daily_volume=2_000_000, avg_dollar_volume=2e8,
                return_pct=0.1, volatility_pct=0.25,
                lookback_days=lookback_days, source="mock",
            )

    with patch.object(r, "build_universe", side_effect=_big_universe), \
         patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_NoOpPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--universe", "full",
            "--top", "5", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0

    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    assert js["mode"] == "full"
    s1 = next(s for s in js["stages"] if s["name"] == "1_universe")
    assert s1["output_count"] == 800, (
        f"Full mode must keep the full 800-row cleaned universe; got {s1['output_count']}."
    )
    # Stage 2 then ranks by liquidity and keeps after_prices_top_k (500 by default).
    s2 = next(s for s in js["stages"] if s["name"] == "2_prices")
    assert s2["input_count"] == 800
    assert s2["output_count"] <= 800  # top-K cap applies here, not in stage 1


def test_custom_mode_uses_provided_tickers(temp_workdir: Path):
    from asset_selection.pipelines import run_asset_selection as r

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "NVDA", "AMD",
            "--top", "2", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0
    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    assert js["mode"] == "custom"
    assert {c["ticker"] for c in js["candidates"]} == {"NVDA", "AMD"}


# ---------------------------------------------------------------------------
# Output-quality fields + post-run validation report (audit fixes)
# ---------------------------------------------------------------------------

def test_pipeline_emits_quality_fields_and_validation_report(temp_workdir: Path):
    """Every candidate must carry the new selection_bucket, richer sentiment
    accounting, and fundamentals explainability fields; the run must also emit
    reports/output_validation.{json,md}."""
    from asset_selection.pipelines import run_asset_selection as r

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "MSFT", "GOOGL",
            "--top", "3", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0

    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    new_fields = {
        "selection_bucket",
        "sentiment_unique_article_count", "sentiment_duplicate_count",
        "sentiment_stale_count", "sentiment_fresh_ratio", "sentiment_unique_ratio",
        "strongest_metric", "weakest_metric",
        "market_cap_available", "valuation_metrics_available",
    }
    for c in js["candidates"]:
        missing = new_fields - set(c.keys())
        assert not missing, f"{c['ticker']} missing new quality fields: {missing}"
        assert c["selection_bucket"], f"{c['ticker']} has empty selection_bucket"

    # AAPL had two distinct articles from two sources -> not full confidence.
    by_ticker = {c["ticker"]: c for c in js["candidates"]}
    assert by_ticker["AAPL"]["sentiment_confidence"] < 1.0

    # The validation report must be written and parseable.
    val_json = temp_workdir / "reports" / "output_validation.json"
    val_md = temp_workdir / "reports" / "output_validation.md"
    assert val_json.exists() and val_md.exists()
    report = json.loads(val_json.read_text())
    assert report["overall_status"] in {"ok", "warn"}
    check_names = {c["name"] for c in report["checks"]}
    assert "excluded_security_types_in_results" in check_names
    assert "overestimated_sentiment_confidence" in check_names


def test_custom_mode_preserves_class_share_ticker(temp_workdir: Path):
    """A class share written in dot notation (BRK.B) must survive the pipeline
    with its canonical spelling intact in the output -- the dot->hyphen mapping
    is a provider-call detail, not a mutation of the canonical ticker."""
    from asset_selection.pipelines import run_asset_selection as r

    class _ClassShareFund:
        def __init__(self, **k): pass
        def fetch(self, ticker):
            # The pipeline passes the canonical ticker through; a real provider
            # would map BRK.B -> BRK-B internally. Echo the canonical spelling.
            return Fundamentals(
                ticker=ticker, company_name=f"{ticker} Holdings",
                market_cap=8e11, sector="Financials",
                revenue_growth=0.1, earnings_growth=0.1, fcf_growth=0.1,
                roe=0.15, roa=0.08, operating_margin=0.25, net_margin=0.18,
                debt_to_equity=30.0, current_ratio=1.4,
                free_cash_flow_yield=0.03, operating_cash_flow_margin=0.2,
                pe_ratio=22.0, forward_pe=20.0, peg_ratio=1.6,
                price_to_sales=4.0, price_to_book=1.5, source="mock",
            )

    with patch("asset_selection.data_providers.get_fundamentals_provider", return_value=_ClassShareFund), \
         patch("asset_selection.data_providers.get_prices_provider", return_value=_PatchedPrices), \
         patch("asset_selection.data_providers.get_news_provider", return_value=_PatchedNews):
        rc = r.main([
            "--config", "configs/default_config.yaml",
            "--tickers", "AAPL", "BRK.B", "BF.B",
            "--top", "3", "--no-cache", "--log-level", "ERROR",
        ])
    assert rc == 0
    js = json.loads((temp_workdir / "reports" / "asset_selection_summary.json").read_text())
    tickers = {c["ticker"] for c in js["candidates"]}
    assert "BRK.B" in tickers and "BF.B" in tickers, (
        f"Class-share canonical spelling was lost: {tickers}"
    )
