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
    profile = profiles.get(ticker, Fundamentals(ticker=ticker, source="mock"))
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
    last, vol, ret, sigma = profiles.get(ticker, (None, None, None, None))
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
    with patch.object(r, "get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch.object(r, "get_prices_provider", return_value=_PatchedPrices), \
         patch.object(r, "get_news_provider", return_value=_PatchedNews):
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

    with patch.object(r, "get_fundamentals_provider", return_value=_PatchedFundamentals), \
         patch.object(r, "get_prices_provider", return_value=_PatchedPrices), \
         patch.object(r, "get_news_provider", return_value=_EmptyNews):
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
