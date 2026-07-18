"""Price-coverage audit: symbol resolution, per-provider attempts, honest
error classification, critical-ticker recovery, and materiality validation.

These tests pin the behaviour that the price-coverage milestone exists to
guarantee:

  * yfinance is queried with direct symbols (NVDA), never Stooq-style (nvda.us);
  * class shares resolve to the right provider spelling (BRK.B -> BRK-B);
  * every symbol/provider attempt is recorded, not collapsed into one verdict;
  * a price miss with surviving fundamentals is a PROVIDER GAP, not a delisting;
  * a missing mega-cap is reported loudly (materiality) instead of being buried
    under 99.8% headline coverage, and downgrades the run to PARTIAL while
    staying trusted (exit 0) -- it never silently vanishes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from asset_selection.config import AppConfig, CriticalTickersConfig
from asset_selection.data_providers import errors as err
from asset_selection.data_providers.base import PriceSnapshot, make_provider_attempt
from asset_selection.data_providers.prices_provider import _run_symbol_ladder
from asset_selection.data_providers.symbols import (
    resolve_provider_symbols,
    stooq_symbol,
    to_provider_symbol,
)
from asset_selection.pipelines.run_asset_selection import (
    StageStats,
    _stage2_recover_critical,
)
from asset_selection.validation import provider_diagnostics as pd_diag
from asset_selection.validation.coverage import (
    COMPLETE,
    COMPLETE_WITH_MINOR_GAPS,
    INVALID_SYSTEMIC_PROVIDER_FAILURE,
    PARTIAL_CRITICAL_TICKER_FAILURE,
    VALID_WITH_MATERIAL_WARNINGS,
    assess_materiality,
    determine_run_status,
)


# ---------------------------------------------------------------------------
# 1. Symbol-resolution ladder
# ---------------------------------------------------------------------------

def test_yfinance_direct_symbols_single_variant():
    # The overwhelming-majority case: a plain symbol costs exactly one attempt
    # and the variant is the canonical symbol itself, NOT a Stooq spelling.
    for sym in ("NVDA", "RDDT", "CRDO", "GEN", "ONDS", "WDC", "CDE", "EQT"):
        variants = resolve_provider_symbols(sym, "yfinance")
        assert variants[0] == sym
        assert variants == [sym]
        assert f"{sym.lower()}.us" not in variants  # never Stooq-style


def test_yfinance_class_shares_use_hyphen_and_keep_dotted_fallback():
    variants = resolve_provider_symbols("BRK.B", "yfinance")
    assert variants[0] == "BRK-B"          # primary spelling yfinance expects
    assert "BRK.B" in variants             # dotted original kept as a fallback
    assert "brk-b.us" not in variants      # never a Stooq symbol


def test_goog_and_googl_stay_distinct_share_classes():
    assert resolve_provider_symbols("GOOG", "yfinance") == ["GOOG"]
    assert resolve_provider_symbols("GOOGL", "yfinance") == ["GOOGL"]


def test_stooq_symbols_are_lowercase_dot_us():
    assert stooq_symbol("NVDA") == "nvda.us"
    assert stooq_symbol("BRK.B") == "brk-b.us"
    variants = resolve_provider_symbols("NVDA", "stooq")
    assert variants[0] == "nvda.us"


def test_stooq_normalization_never_leaks_into_yfinance():
    # Regression guard for the core bug: a Stooq spelling must never be tried as
    # a yfinance symbol.
    y = resolve_provider_symbols("NVDA", "yfinance")
    s = resolve_provider_symbols("NVDA", "stooq")
    assert "nvda.us" in s and "nvda.us" not in y
    assert to_provider_symbol("NVDA", "yfinance") == "NVDA"


# ---------------------------------------------------------------------------
# 2. Per-provider / per-symbol attempt trail
# ---------------------------------------------------------------------------

def _snap(ticker="NVDA"):
    return PriceSnapshot(ticker=ticker, lookback_days=90, source="yfinance")


def test_ladder_records_single_success_attempt():
    snap = _snap()
    df = pd.DataFrame({"Close": [1.0, 2.0], "Volume": [10, 20]})
    hist, used, exc = _run_symbol_ladder(
        "NVDA", ["NVDA"], "yfinance", lambda sym, _d: df, 90, snap,
    )
    assert exc is None and used == "NVDA"
    assert len(snap.provider_attempts) == 1
    a = snap.provider_attempts[0]
    assert a["success"] is True
    assert a["provider_symbol"] == "NVDA"
    assert a["provider_name"] == "yfinance"


def test_ladder_records_one_attempt_per_empty_variant():
    snap = _snap("BRK.B")
    empty = pd.DataFrame()
    hist, used, exc = _run_symbol_ladder(
        "BRK.B", ["BRK-B", "BRK.B"], "yfinance", lambda sym, _d: empty, 90, snap,
    )
    assert hist is None and exc is None
    assert [a["provider_symbol"] for a in snap.provider_attempts] == ["BRK-B", "BRK.B"]
    assert all(a["success"] is False for a in snap.provider_attempts)


def test_ladder_stops_after_transport_exception():
    snap = _snap()

    def boom(sym, _d):
        raise ConnectionError("blocked")

    hist, used, exc = _run_symbol_ladder(
        "NVDA", ["NVDA", "NVDA-ALT"], "yfinance", boom, 90, snap,
    )
    assert isinstance(exc, ConnectionError)
    # A different spelling cannot fix a blocked provider -> stop after one try.
    assert len(snap.provider_attempts) == 1
    assert snap.provider_attempts[0]["success"] is False


# ---------------------------------------------------------------------------
# 3. Honest reclassification of a price failure
# ---------------------------------------------------------------------------

def test_reclassify_leaves_provider_side_faults_unchanged():
    # A transport/blocked fault is NOT a per-ticker coverage gap; keep it as-is
    # so systemic counting is not distorted.
    assert (
        err.reclassify_price_failure(err.PROVIDER_JSON_PARSE_ERROR, has_other_data=False)
        == err.PROVIDER_JSON_PARSE_ERROR
    )


def test_reclassify_price_gap_when_fundamentals_exist():
    # Fundamentals survive -> a price-feed coverage gap, explicitly NOT delisting.
    assert (
        err.reclassify_price_failure("NO_PRICE_DATA", has_other_data=True)
        == err.PRICE_PROVIDER_GAP
    )


def test_reclassify_coverage_gap_when_all_variants_empty():
    assert (
        err.reclassify_price_failure(
            "NO_PRICE_DATA", has_other_data=False, all_variants_empty=True
        )
        == err.PROVIDER_COVERAGE_GAP
    )


def test_reclassify_default_is_endpoint_no_data_not_delisted():
    out = err.reclassify_price_failure("NO_PRICE_DATA")
    assert out == err.PRICE_ENDPOINT_NO_DATA
    assert out != "POSSIBLY_DELISTED"


# ---------------------------------------------------------------------------
# 4. Stage-2 critical-ticker recovery
# ---------------------------------------------------------------------------

class _FundStub:
    """Minimal fundamentals provider: returns a fixed status/market_cap."""

    def __init__(self, status="ok", market_cap=2_000_000_000_000.0):
        self._status = status
        self._market_cap = market_cap
        self.calls = []

    def fetch(self, ticker):
        self.calls.append(ticker)
        return SimpleNamespace(status=self._status, market_cap=self._market_cap)


def _empty_price(ticker="NVDA", attempts=None):
    snap = PriceSnapshot(
        ticker=ticker, lookback_days=90, source="yfinance",
        provider_symbol=ticker, status="empty",
        error="no rows", error_type="NO_PRICE_DATA",
    )
    snap.provider_attempts = attempts if attempts is not None else [
        make_provider_attempt(
            canonical_symbol=ticker, provider_name="yfinance",
            provider_symbol=ticker, success=False,
            error_type="NO_PRICE_DATA", error_message="empty payload",
        ),
        make_provider_attempt(
            canonical_symbol=ticker, provider_name="stooq",
            provider_symbol=f"{ticker.lower()}.us", success=False,
            error_type="NO_PRICE_DATA", error_message="empty payload",
        ),
    ]
    return snap


def test_recovery_confirms_company_real_and_labels_price_gap():
    cfg = AppConfig()
    stats = StageStats(name="2_prices")
    records = {"NVDA": _empty_price("NVDA")}
    fund = _FundStub(status="ok", market_cap=2_000_000_000_000.0)

    _stage2_recover_critical(records, stats, cfg, fund, {"NVDA"})

    assert len(stats.material_gaps) == 1
    gap = stats.material_gaps[0]
    assert gap["ticker"] == "NVDA"
    assert gap["company_confirmed_real"] is True
    assert gap["cross_provider_fundamentals"] == "ok"
    assert gap["reclassified_error_type"] == err.PRICE_PROVIDER_GAP
    assert gap["is_large_cap"] is True
    # The per-provider trail is carried into the gap (both providers visible).
    provs = {a["provider_name"] for a in gap["provider_attempts"]}
    assert provs == {"yfinance", "stooq"}
    # Honest language: never asserts delisting.
    assert "NOT delisting" in gap["reason"]
    assert "delisted" not in gap["reason"].lower().replace("not delisting", "")


def test_recovery_skips_priced_and_noncritical_tickers():
    cfg = AppConfig()
    stats = StageStats(name="2_prices")
    ok = PriceSnapshot(ticker="AAPL", lookback_days=90, source="yfinance", status="ok")
    records = {"AAPL": ok, "NVDA": _empty_price("NVDA")}
    fund = _FundStub()

    # Only AAPL is "critical" here, but AAPL priced fine -> no gap; NVDA failed
    # but is not in the critical set passed in -> not investigated.
    _stage2_recover_critical(records, stats, cfg, fund, {"AAPL"})
    assert stats.material_gaps == []
    assert fund.calls == []  # priced-ok ticker never triggers a confirm call


def test_recovery_without_fundamentals_does_not_assert_delisting():
    cfg = AppConfig()
    stats = StageStats(name="2_prices")
    records = {"NVDA": _empty_price("NVDA")}

    _stage2_recover_critical(records, stats, cfg, None, {"NVDA"})
    gap = stats.material_gaps[0]
    assert gap["company_confirmed_real"] is False
    assert gap["cross_provider_fundamentals"] == "skipped"
    # All variants came back empty (non provider-side) -> coverage gap, hedged.
    assert gap["reclassified_error_type"] == err.PROVIDER_COVERAGE_GAP
    assert "not asserted delisted" in gap["reason"]


# ---------------------------------------------------------------------------
# 5. Materiality validation + completeness statuses
# ---------------------------------------------------------------------------

def _price_stage_with_gaps(gaps, provider_failures=None):
    stats = StageStats(name="2_prices")
    stats.input_count = 500
    stats.provider_failures = (
        provider_failures if provider_failures is not None else len(gaps)
    )
    for g in gaps:
        stats.record_material_gap(g)
    return stats


def _gap(ticker, *, is_large_cap=True, is_user_watchlist=False, confirmed=True):
    return {
        "ticker": ticker,
        "provider_symbol": ticker,
        "original_error_type": "NO_PRICE_DATA",
        "reclassified_error_type": err.PRICE_PROVIDER_GAP,
        "cross_provider_fundamentals": "ok" if confirmed else "skipped",
        "company_confirmed_real": confirmed,
        "is_large_cap": is_large_cap,
        "is_user_watchlist": is_user_watchlist,
        "all_symbol_variants_empty": True,
        "provider_attempts": [],
        "reason": "price-provider gap, NOT delisting",
    }


def _ranked(tickers):
    return pd.DataFrame({"ticker": tickers, "market_cap": [1e9] * len(tickers)})


def test_hard_critical_failure_is_partial_critical_status():
    cfg = AppConfig()  # NVDA is in the static set
    stage = _price_stage_with_gaps([_gap("NVDA")])
    mat = assess_materiality([stage], _ranked(["AAPL", "MSFT"]), cfg)

    assert mat["ranking_completeness_status"] == PARTIAL_CRITICAL_TICKER_FAILURE
    assert mat["critical_ticker_failures"] == ["NVDA"]
    assert mat["hard_critical_ticker_failures"] == ["NVDA"]
    assert mat["failed_large_cap_tickers"] == ["NVDA"]
    assert mat["critical_failures_with_company_confirmed_real"] == ["NVDA"]


def test_watchlist_only_failure_is_material_warning_not_partial():
    cfg = AppConfig(
        critical_tickers=CriticalTickersConfig(
            static_tickers=["AAPL"], user_watchlist=["RDDT"],
            treat_benchmark_as_critical=False,
        )
    )
    stage = _price_stage_with_gaps(
        [_gap("RDDT", is_large_cap=False, is_user_watchlist=True)]
    )
    mat = assess_materiality([stage], _ranked(["AAPL"]), cfg)

    assert mat["ranking_completeness_status"] == VALID_WITH_MATERIAL_WARNINGS
    assert mat["hard_critical_ticker_failures"] == []
    assert mat["failed_user_watchlist_tickers"] == ["RDDT"]


def test_gap_ticker_recovered_into_ranking_is_not_counted():
    cfg = AppConfig()
    # NVDA failed price but ended up in the ranking (e.g. rescued by cache):
    # it is no longer a live gap.
    stage = _price_stage_with_gaps([_gap("NVDA")], provider_failures=1)
    mat = assess_materiality([stage], _ranked(["NVDA", "AAPL"]), cfg)

    assert mat["critical_ticker_failures"] == []
    assert mat["ranking_completeness_status"] in (COMPLETE, COMPLETE_WITH_MINOR_GAPS)


def test_minor_nonmaterial_gaps_only():
    cfg = AppConfig()
    # Price failures exist but none are critical -> minor gaps, ranking complete.
    stage = _price_stage_with_gaps([], provider_failures=3)
    mat = assess_materiality([stage], _ranked(["AAPL", "MSFT"]), cfg)
    assert mat["ranking_completeness_status"] == COMPLETE_WITH_MINOR_GAPS
    assert mat["minor_non_material_price_gaps"] == 3


def test_blocking_systemic_failure_dominates_completeness():
    cfg = AppConfig()
    stage = _price_stage_with_gaps([_gap("NVDA")])
    mat = assess_materiality(
        [stage], _ranked(["AAPL"]), cfg,
        health_report={"any_blocking_systemic_failure": True},
    )
    assert mat["ranking_completeness_status"] == INVALID_SYSTEMIC_PROVIDER_FAILURE


# ---------------------------------------------------------------------------
# 6. Run-status downgrade: VALID coverage but a missing mega-cap
# ---------------------------------------------------------------------------

def _healthy_coverage():
    return {
        "thresholds": {},
        "price": {"coverage_ratio": 0.998},
        "fundamentals": {"coverage_ratio": 1.0},
        "news": {"coverage_ratio": 1.0},
        "provider_failure_ratio": 0.0,
        "ranked_candidates": 150,
        "valid_candidates": 150,
        "meets_price_coverage": True,
        "meets_fundamentals_coverage": True,
        "meets_news_coverage": True,
        "within_provider_failure_budget": True,
        "has_min_valid_candidates": True,
    }


def test_critical_miss_downgrades_valid_to_partial_but_stays_trusted():
    cfg = AppConfig()
    stage = _price_stage_with_gaps([_gap("NVDA")])
    mat = assess_materiality([stage], _ranked(["AAPL", "MSFT"]), cfg)

    status = determine_run_status(_healthy_coverage(), None, cfg, mat)

    # The coverage axis is still a VALID_RANKING ...
    assert status["ranking_validity"] == "VALID_RANKING"
    # ... but the headline rollup is downgraded so the gap can't be missed ...
    assert status["run_status"] == "PARTIAL"
    assert status["ranking_completeness_status"] == PARTIAL_CRITICAL_TICKER_FAILURE
    # ... while staying a trusted, actionable run (exit 0).
    assert status["is_trusted"] is True
    assert status["return_code"] == 0
    # The missing mega-cap is named loudly in the warnings.
    assert any("NVDA" in w for w in status["warnings"])
    assert status["critical_ticker_failures"] == ["NVDA"]


def test_clean_run_stays_valid_with_no_materiality():
    cfg = AppConfig()
    status = determine_run_status(_healthy_coverage(), None, cfg, None)
    assert status["run_status"] == "VALID"
    assert status["ranking_validity"] == "VALID_RANKING"
    assert status["ranking_completeness_status"] == COMPLETE


# ---------------------------------------------------------------------------
# 7. Diagnostics rendering shows the per-provider attempt trail
# ---------------------------------------------------------------------------

def test_diagnostics_render_material_gaps_and_attempts():
    cfg = AppConfig()
    stage = _price_stage_with_gaps(
        [{
            "ticker": "NVDA",
            "provider_symbol": "NVDA",
            "original_error_type": "NO_PRICE_DATA",
            "reclassified_error_type": err.PRICE_PROVIDER_GAP,
            "cross_provider_fundamentals": "ok",
            "company_confirmed_real": True,
            "is_large_cap": True,
            "is_user_watchlist": False,
            "all_symbol_variants_empty": True,
            "provider_attempts": [
                make_provider_attempt(
                    canonical_symbol="NVDA", provider_name="yfinance",
                    provider_symbol="NVDA", success=False,
                    error_type="NO_PRICE_DATA", error_message="empty payload",
                ),
                make_provider_attempt(
                    canonical_symbol="NVDA", provider_name="stooq",
                    provider_symbol="nvda.us", success=False,
                    error_type="NO_PRICE_DATA", error_message="empty payload",
                ),
            ],
            "reason": "price-provider gap, NOT delisting",
        }]
    )
    mat = assess_materiality([stage], _ranked(["AAPL"]), cfg)
    status = determine_run_status(_healthy_coverage(), None, cfg, mat)

    diag = pd_diag.build_provider_diagnostics(
        status=status, coverage=_healthy_coverage(), health_report=None,
        provider_failures={}, fallback_usage={}, cache_usage={},
        providers={"prices": "yfinance"},
    )
    md = pd_diag._render_markdown(diag)

    assert "Material data gaps" in md
    assert "PARTIAL_CRITICAL_TICKER_FAILURE" in md
    assert "provider attempt trail" in md
    assert "nvda.us" in md          # the Stooq symbol that was tried
    assert "PRICE_PROVIDER_GAP" in md
    assert "yfinance" in md and "stooq" in md
    # The diag dict is JSON-serialisable (it is written to disk).
    import json
    json.loads(json.dumps(diag, default=str))


def test_run_status_banner_surfaces_completeness():
    status = {
        "run_status": "PARTIAL",
        "ranking_validity": "VALID_RANKING",
        "ranking_completeness_status": PARTIAL_CRITICAL_TICKER_FAILURE,
        "is_trusted": True,
        "hard_critical_ticker_failures": ["NVDA"],
        "critical_ticker_failures": ["NVDA"],
    }
    banner = pd_diag.render_run_status_banner(status)
    assert "PARTIAL_CRITICAL_TICKER_FAILURE" in banner
    assert "NVDA" in banner
