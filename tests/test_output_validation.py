"""Post-run output validation checks."""
from __future__ import annotations

import json

import pandas as pd

from asset_selection.config import AppConfig
from asset_selection.validation import validate_outputs, write_validation_reports


def _cfg() -> AppConfig:
    return AppConfig()


def _clean_row(**overrides):
    row = {
        "ticker": "AAA",
        "company_name": "Alpha Industries Inc",
        "market_cap": 1.0e11,
        "market_cap_available": True,
        "volatility_pct": 0.30,
        "return_pct": 0.10,
        "flags": [],
        "selection_bucket": "high_quality_core_candidate",
        "article_count": 8,
        "unique_article_count": 8,
        "stale_count": 0,
        "fresh_ratio": 1.0,
        "source_diversity": 4,
        "sentiment_confidence": 0.5,
        "growth_score": 60.0,
        "quality_score": 62.0,
        "valuation_score": 58.0,
        "balance_sheet_score": 59.0,
        "cash_flow_score": 61.0,
    }
    row.update(overrides)
    return row


def test_clean_run_reports_all_ok():
    ranked = pd.DataFrame([_clean_row()])
    report = validate_outputs(ranked, {"provider_failures": {"total": 0}}, _cfg())
    assert report["overall_status"] == "ok"
    assert report["n_warnings"] == 0
    assert all(c["status"] == "ok" for c in report["checks"])


def test_excluded_security_type_leak_is_flagged():
    ranked = pd.DataFrame([_clean_row(ticker="XYZ", company_name="Big Index ETF Trust")])
    report = validate_outputs(ranked, {}, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "excluded_security_types_in_results")
    assert check["status"] == "warn"
    assert check["examples"][0]["ticker"] == "XYZ"


def test_extreme_volatility_is_flagged():
    ranked = pd.DataFrame([_clean_row(
        ticker="ONDS", volatility_pct=1.10, flags=["HIGH_VOLATILITY"],
        selection_bucket="speculative_candidate",
    )])
    report = validate_outputs(ranked, {}, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "extreme_volatility")
    assert check["status"] == "warn"
    assert check["count"] == 1


def test_overestimated_confidence_is_flagged():
    # Confidence 1.0 off a single-source, thin feed -> overstated.
    ranked = pd.DataFrame([_clean_row(
        sentiment_confidence=1.0, unique_article_count=5, source_diversity=1,
    )])
    report = validate_outputs(ranked, {}, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "overestimated_sentiment_confidence")
    assert check["status"] == "warn"


def test_missing_market_cap_is_flagged():
    ranked = pd.DataFrame([_clean_row(
        market_cap=float("nan"), market_cap_available=False, flags=["MISSING_MARKET_CAP"],
    )])
    report = validate_outputs(ranked, {}, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "missing_market_cap")
    assert check["status"] == "warn"


def test_single_pillar_dominance_is_flagged():
    ranked = pd.DataFrame([_clean_row(
        growth_score=85.0, quality_score=40.0, valuation_score=38.0,
        balance_sheet_score=41.0, cash_flow_score=39.0,
    )])
    report = validate_outputs(ranked, {}, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "single_pillar_dominance")
    assert check["status"] == "warn"
    assert check["examples"][0]["dominant_pillar"] == "growth"


def test_provider_failures_surface_from_summary():
    ranked = pd.DataFrame([_clean_row()])
    summary = {"provider_failures": {"total": 3, "by_reason": {"empty": 3}, "examples": []}}
    report = validate_outputs(ranked, summary, _cfg())
    check = next(c for c in report["checks"] if c["name"] == "provider_failures")
    assert check["status"] == "warn"
    assert check["count"] == 0  # no per-row examples, but the block still warns
    assert "3 provider failure" in check["message"]


def test_write_reports_creates_json_and_md(tmp_path):
    ranked = pd.DataFrame([_clean_row()])
    report = validate_outputs(ranked, {}, _cfg())
    json_path, md_path = write_validation_reports(report, tmp_path)
    assert json_path.exists() and md_path.exists()
    loaded = json.loads(json_path.read_text())
    assert loaded["overall_status"] in {"ok", "warn"}
    assert "# Output Validation" in md_path.read_text()
