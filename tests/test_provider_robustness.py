"""Robustness layer: error taxonomy, health checks, fallback, coverage gating.

These are the unit tests behind the audit's central promise -- that the pipeline
can tell a *partial ticker failure* apart from a *systemic provider outage*, and
refuses to dress up an outage as a trustworthy ranking. They are pure/hermetic
(no network), exercising the classifier, the benchmark health probe, the
provider-fallback ladder, the coverage->run-status decision tree, and the
diagnostics renderer directly.
"""
from __future__ import annotations

import pandas as pd
import pytest

from asset_selection.config import AppConfig
from asset_selection.data_providers import errors as err
from asset_selection.data_providers.base import (
    Fundamentals,
    NewsItem,
    PriceSnapshot,
)
from asset_selection.data_providers.fallback import (
    FallbackNewsProvider,
    FallbackPricesProvider,
)
from asset_selection.data_providers.symbols import to_provider_symbol
from asset_selection.health.provider_health import (
    BENCHMARK_TICKERS,
    run_provider_health_checks,
)
from asset_selection.pipelines.run_asset_selection import StageStats
from asset_selection.utils.cache import Cache
from asset_selection.validation import (
    DIAGNOSTIC_ONLY,
    INVALID_INSUFFICIENT_DATA,
    INVALID_PROVIDER_FAILURE,
    PARTIAL_RANKING_WITH_WARNINGS,
    VALID_RANKING,
    assess_coverage,
    build_provider_diagnostics,
    determine_run_status,
    is_trusted_run_status,
    render_run_status_banner,
    return_code_for,
    write_provider_diagnostics,
)


# ===========================================================================
# 1. Error taxonomy / classification
# ===========================================================================

def test_json_parse_block_is_provider_side_not_a_ticker_problem():
    """The live yfinance block returns 'Expecting value: line 1 ...'. That must
    classify as a PROVIDER-side JSON-parse error, NOT an invalid ticker."""
    et = err.classify_exception(ValueError("Expecting value: line 1 column 1 (char 0)"))
    assert et == err.PROVIDER_JSON_PARSE_ERROR
    assert err.is_provider_side(et) is True
    assert et != err.INVALID_TICKER


def test_classify_exception_buckets_transport_failures():
    assert err.classify_exception(Exception("HTTP 429 Too Many Requests")) == err.PROVIDER_RATE_LIMITED
    assert err.classify_exception(TimeoutError("read timed out")) == err.PROVIDER_TIMEOUT
    assert err.classify_exception(Exception("403 Forbidden")) == err.PROVIDER_BLOCKED
    assert err.classify_exception(Exception("502 Bad Gateway")) == err.PROVIDER_HTTP_ERROR
    # Anything unrecognised is a provider-side UNKNOWN, never a ticker problem.
    assert err.classify_exception(Exception("???")) == err.PROVIDER_UNKNOWN_ERROR
    assert err.is_provider_side(err.PROVIDER_UNKNOWN_ERROR) is True


def test_classify_empty_is_per_data_type_and_not_provider_side():
    assert err.classify_empty("price") == err.NO_PRICE_DATA
    assert err.classify_empty("fundamentals") == err.NO_FUNDAMENTAL_DATA
    assert err.classify_empty("news") == err.NO_NEWS_DATA
    for et in (err.NO_PRICE_DATA, err.NO_FUNDAMENTAL_DATA, err.NO_NEWS_DATA):
        assert err.is_provider_side(et) is False


def test_classifiers_never_assert_delisting():
    """A bare empty / parse error must never be reported as POSSIBLY_DELISTED:
    that needs corroborating evidence the classifiers don't have."""
    assert err.classify_exception(ValueError("Expecting value")) != err.POSSIBLY_DELISTED
    assert err.classify_error_text("some empty response") != err.POSSIBLY_DELISTED
    assert err.classify_empty("price") != err.POSSIBLY_DELISTED


# ===========================================================================
# 2. Provider health checks on benchmark mega-caps
# ===========================================================================

class _StubPriceProvider:
    name = "stub-prices"

    def __init__(self, behavior):
        self.behavior = behavior  # ticker -> "ok" | "raise" | "empty"

    def fetch(self, ticker, lookback_days=90):
        b = self.behavior.get(ticker, "ok")
        if b == "raise":
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        if b == "empty":
            return PriceSnapshot(ticker=ticker, status="empty", error="no data")
        return PriceSnapshot(
            ticker=ticker, last_close=100.0, avg_dollar_volume=1e9, status="ok"
        )


class _StubFundProvider:
    name = "stub-fund"

    def __init__(self, behavior):
        self.behavior = behavior

    def fetch(self, ticker):
        b = self.behavior.get(ticker, "ok")
        if b == "raise":
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return Fundamentals(ticker=ticker, company_name=f"{ticker} Inc", market_cap=1e11, status="ok")


class _StubNewsProvider:
    name = "stub-news"

    def __init__(self, behavior):
        self.behavior = behavior

    def fetch(self, ticker, max_age_days=30):
        b = self.behavior.get(ticker, "ok")
        if b == "raise":
            raise TimeoutError("read timed out")
        if b == "empty":
            return []
        return [NewsItem(ticker=ticker, headline="h", summary="s", source="W", url="u")]


def _all(behavior_value):
    return {t: behavior_value for t in BENCHMARK_TICKERS}


def test_health_check_healthy_when_all_benchmarks_resolve():
    report = run_provider_health_checks(
        price_provider=_StubPriceProvider(_all("ok")),
        fundamentals_provider=_StubFundProvider(_all("ok")),
        news_provider=_StubNewsProvider(_all("ok")),
    )
    assert report["overall_status"] == "healthy"
    assert report["any_blocking_systemic_failure"] is False
    assert report["price_systemic_failure"] is False


def test_price_bellwether_outage_is_systemic_not_invalid_tickers():
    """AAPL/MSFT/GOOGL all failing on a JSON-parse block is a provider outage."""
    report = run_provider_health_checks(
        price_provider=_StubPriceProvider(_all("raise")),
    )
    assert report["price_systemic_failure"] is True
    assert report["any_blocking_systemic_failure"] is True
    assert report["overall_status"] == "systemic_failure"
    # The recorded failure is provider-side, never INVALID_TICKER.
    price_block = report["by_data_type"]["price"]
    assert err.PROVIDER_JSON_PARSE_ERROR in price_block["error_types"]
    assert err.INVALID_TICKER not in price_block["error_types"]


def test_single_odd_name_miss_is_not_systemic():
    """One benchmark with no price data (bellwethers fine) must NOT be systemic."""
    behavior = _all("ok")
    behavior["NVDA"] = "empty"  # NO_PRICE_DATA, not provider-side
    report = run_provider_health_checks(price_provider=_StubPriceProvider(behavior))
    assert report["price_systemic_failure"] is False
    assert report["any_blocking_systemic_failure"] is False
    assert report["overall_status"] == "degraded"


def test_news_outage_is_never_systemic_blocking():
    report = run_provider_health_checks(news_provider=_StubNewsProvider(_all("raise")))
    assert report["by_data_type"]["news"]["systemic_failure"] is False
    assert report["any_blocking_systemic_failure"] is False


def test_all_fundamentals_provider_side_is_systemic():
    report = run_provider_health_checks(
        fundamentals_provider=_StubFundProvider(_all("raise")),
    )
    assert report["fundamentals_systemic_failure"] is True
    assert report["any_blocking_systemic_failure"] is True


# ===========================================================================
# 3. Provider fallback ladder (Plan A -> B -> C -> D)
# ===========================================================================

class _StubChainPrices:
    """A record-returning prices provider (never raises) for chain tests."""

    cache_namespace = "prices"

    def __init__(self, name, result, cache=None):
        self.name = name
        self._result = result
        self.cache = cache

    def cache_identifier(self, ticker, lookback_days=90):
        return f"{ticker}:{lookback_days}"

    def fetch(self, ticker, lookback_days=90):
        return self._result


def _ok_snap(ticker="AAPL", last=100.0):
    return PriceSnapshot(ticker=ticker, last_close=last, avg_dollar_volume=1e9, status="ok")


def _err_snap(ticker="AAPL"):
    return PriceSnapshot(ticker=ticker, status="error", error="blocked")


def test_fallback_plan_a_uses_live_primary():
    fb = FallbackPricesProvider([_StubChainPrices("yfinance", _ok_snap())])
    rec = fb.fetch("AAPL")
    assert rec.status == "ok"
    assert rec.data_source == "live"
    assert fb.usage["primary"] == 1
    assert fb.usage["fallback"] == 0


def test_fallback_plan_b_uses_secondary_when_primary_fails():
    """yfinance blocked -> the Stooq-shaped backup is used and labeled fallback."""
    primary = _StubChainPrices("yfinance", _err_snap())
    secondary = _StubChainPrices("stooq", _ok_snap(last=123.0))
    fb = FallbackPricesProvider([primary, secondary])
    rec = fb.fetch("AAPL")
    assert rec.last_close == 123.0
    assert rec.data_source == "fallback"
    assert fb.usage["fallback"] == 1
    assert fb.usage["by_provider"].get("stooq") == 1
    assert fb.provider_names == ["yfinance", "stooq"]


def test_fallback_plan_c_serves_fresh_cache_labeled_stale(tmp_path):
    """When every live provider fails AND cache-backup is on, a fresh-enough
    cache entry is served -- explicitly labeled stale_cache, never as live."""
    cache = Cache(directory=str(tmp_path / "cache"), enabled=True)
    cached = _ok_snap(last=200.0)
    cache.set("prices", "AAPL:90", cached.__dict__)

    primary = _StubChainPrices("yfinance", _err_snap(), cache=cache)
    fb = FallbackPricesProvider(
        [primary], use_cache_on_failure=True, max_cache_age_seconds=7 * 86400
    )
    rec = fb.fetch("AAPL")
    assert rec.last_close == 200.0
    assert rec.data_source == "stale_cache"
    assert fb.usage["stale_cache"] == 1


def test_fallback_plan_d_honest_unavailable_when_all_fail(tmp_path):
    """No live data and no usable cache -> an honest 'unavailable' record."""
    primary = _StubChainPrices("yfinance", _err_snap())
    fb = FallbackPricesProvider([primary], use_cache_on_failure=False)
    rec = fb.fetch("AAPL")
    assert rec.data_source == "unavailable"
    assert fb.usage["unavailable"] == 1


class _StubChainNews:
    cache_namespace = "news"

    def __init__(self, name, items=None, exc=None, cache=None):
        self.name = name
        self._items = items
        self._exc = exc
        self.cache = cache

    def fetch(self, ticker, max_age_days=30):
        if self._exc is not None:
            raise self._exc
        return self._items or []


def test_news_fallback_uses_secondary_when_primary_raises():
    primary = _StubChainNews("yfinance", exc=TimeoutError("read timed out"))
    secondary = _StubChainNews(
        "newsapi", items=[NewsItem(ticker="AAPL", headline="h", source="W", url="u")]
    )
    fb = FallbackNewsProvider([primary, secondary])
    items = fb.fetch("AAPL")
    assert len(items) == 1
    assert fb.usage["fallback"] == 1


def test_news_chain_reraises_when_every_provider_fails():
    primary = _StubChainNews("yfinance", exc=TimeoutError("read timed out"))
    secondary = _StubChainNews("newsapi", exc=ValueError("403 Forbidden"))
    fb = FallbackNewsProvider([primary, secondary])
    with pytest.raises(Exception):
        fb.fetch("AAPL")
    assert fb.usage["unavailable"] == 1


# ===========================================================================
# 4. Coverage gating -> run-status decision tree
# ===========================================================================

def _cfg(**robustness_overrides) -> AppConfig:
    cfg = AppConfig()
    for k, v in robustness_overrides.items():
        setattr(cfg.robustness, k, v)
    return cfg


def _stage(name, attempted, provider_failures=0, error_types=None):
    s = StageStats(name=name)
    s.input_count = attempted
    s.provider_failures = provider_failures
    s.failure_error_types = error_types or {}
    return s


def _healthy_stages():
    return [
        _stage("2_prices", 100, 0),
        _stage("3_fundamentals", 100, 0),
        _stage("4_sentiment", 100, 0),
    ]


def _ranked(n_valid=3):
    return pd.DataFrame({"ticker": [f"T{i}" for i in range(n_valid)],
                         "market_cap": [1e9 * (i + 1) for i in range(n_valid)]})


def test_status_mapping_helpers():
    assert return_code_for("VALID") == 0
    assert return_code_for("PARTIAL") == 0
    assert return_code_for("DIAGNOSTIC") == 2
    assert return_code_for("INVALID") == 2
    assert is_trusted_run_status("VALID") and is_trusted_run_status("PARTIAL")
    assert not is_trusted_run_status("DIAGNOSTIC")
    assert not is_trusted_run_status("INVALID")


def test_healthy_coverage_is_valid_ranking():
    cfg = _cfg()
    cov = assess_coverage(_healthy_stages(), _ranked(), cfg)
    status = determine_run_status(cov, None, cfg)
    assert status["ranking_validity"] == VALID_RANKING
    assert status["run_status"] == "VALID"
    assert status["return_code"] == 0
    assert status["is_trusted"] is True


def test_degraded_coverage_is_partial_when_allowed():
    cfg = _cfg(allow_partial_ranking=True)
    # Price coverage 0.4 (< 0.60) but failures are honest NO_PRICE_DATA, so the
    # provider-failure budget is fine -> degraded-but-rankable.
    stages = [
        _stage("2_prices", 100, 60, {"NO_PRICE_DATA": 60}),
        _stage("3_fundamentals", 100, 0),
        _stage("4_sentiment", 100, 0),
    ]
    cov = assess_coverage(stages, _ranked(), cfg)
    status = determine_run_status(cov, None, cfg)
    assert status["ranking_validity"] == PARTIAL_RANKING_WITH_WARNINGS
    assert status["return_code"] == 0
    assert status["invalid_ranking_reasons"]  # records WHY it's only partial


def test_degraded_coverage_is_diagnostic_when_partial_disabled():
    cfg = _cfg(allow_partial_ranking=False)
    stages = [
        _stage("2_prices", 100, 60, {"NO_PRICE_DATA": 60}),
        _stage("3_fundamentals", 100, 0),
        _stage("4_sentiment", 100, 0),
    ]
    cov = assess_coverage(stages, _ranked(), cfg)
    status = determine_run_status(cov, None, cfg)
    assert status["ranking_validity"] == DIAGNOSTIC_ONLY
    assert status["return_code"] == 2
    assert status["is_trusted"] is False


def test_systemic_health_failure_is_invalid_provider_failure():
    cfg = _cfg(stop_on_systemic_provider_failure=True)
    cov = assess_coverage(_healthy_stages(), _ranked(), cfg)
    health = {"any_blocking_systemic_failure": True,
              "price_systemic_failure": True,
              "fundamentals_systemic_failure": False,
              "by_data_type": {}}
    status = determine_run_status(cov, health, cfg)
    assert status["ranking_validity"] == INVALID_PROVIDER_FAILURE
    assert status["return_code"] == 2
    # The reason must name it an outage, never 'invalid tickers'.
    joined = " ".join(status["invalid_ranking_reasons"]).lower()
    assert "outage" in joined or "systemic" in joined


def test_systemic_failure_downgrades_to_diagnostic_when_stop_disabled():
    cfg = _cfg(stop_on_systemic_provider_failure=False)
    cov = assess_coverage(_healthy_stages(), _ranked(), cfg)
    health = {"any_blocking_systemic_failure": True,
              "price_systemic_failure": True,
              "fundamentals_systemic_failure": False,
              "by_data_type": {}}
    status = determine_run_status(cov, health, cfg)
    assert status["ranking_validity"] == DIAGNOSTIC_ONLY
    assert status["return_code"] == 2


def test_no_valid_candidates_is_insufficient_data():
    cfg = _cfg()
    # Ranked rows exist but none carry a real market cap -> thin candidates.
    thin = pd.DataFrame({"ticker": ["A", "B"], "market_cap": [None, None]})
    cov = assess_coverage(_healthy_stages(), thin, cfg)
    status = determine_run_status(cov, None, cfg)
    assert status["ranking_validity"] == INVALID_INSUFFICIENT_DATA
    assert status["return_code"] == 2


def test_news_shortfall_warns_but_keeps_ranking_valid():
    cfg = _cfg()
    stages = [
        _stage("2_prices", 100, 0),
        _stage("3_fundamentals", 100, 0),
        _stage("4_sentiment", 100, 95, {"NO_NEWS_DATA": 95}),  # 0.05 < 0.10
    ]
    cov = assess_coverage(stages, _ranked(), cfg)
    assert cov["meets_news_coverage"] is False
    status = determine_run_status(cov, None, cfg)
    # News never blocks: still a valid ranking, but a warning is recorded.
    assert status["ranking_validity"] == VALID_RANKING
    assert any("news" in w.lower() for w in status["warnings"])


def test_provider_failure_budget_breach_is_provider_side_only():
    """A high rate of PROVIDER-side failures (outage) trips the budget; the same
    count of honest NO_*_DATA misses does not."""
    cfg = _cfg()
    # The budget is provider-side failures / (price + fundamentals attempts).
    # A blanket outage hits both data types provider-side.
    outage = [
        _stage("2_prices", 100, 90, {"PROVIDER_JSON_PARSE_ERROR": 90}),
        _stage("3_fundamentals", 100, 90, {"PROVIDER_JSON_PARSE_ERROR": 90}),
        _stage("4_sentiment", 100, 0),
    ]
    cov = assess_coverage(outage, _ranked(), cfg)
    assert cov["within_provider_failure_budget"] is False
    assert cov["provider_failure_ratio"] > 0.5

    # The same volume of HONEST 'no data' misses must NOT trip the budget.
    honest = [
        _stage("2_prices", 100, 90, {"NO_PRICE_DATA": 90}),
        _stage("3_fundamentals", 100, 90, {"NO_FUNDAMENTAL_DATA": 90}),
        _stage("4_sentiment", 100, 0),
    ]
    cov_honest = assess_coverage(honest, _ranked(), cfg)
    assert cov_honest["within_provider_failure_budget"] is True
    assert cov_honest["provider_failure_ratio"] == 0.0


# ===========================================================================
# 5. Diagnostics report + run-status banner
# ===========================================================================

def test_banner_flags_untrusted_runs_loudly():
    cfg = _cfg()
    cov = assess_coverage(_healthy_stages(), _ranked(), cfg)
    valid = determine_run_status(cov, None, cfg)
    valid_banner = render_run_status_banner(valid, cov)
    assert "VALID" in valid_banner
    assert "NOT A TRUSTED RANKING" not in valid_banner

    invalid = determine_run_status(
        cov, {"any_blocking_systemic_failure": True, "price_systemic_failure": True,
              "fundamentals_systemic_failure": False, "by_data_type": {}}, cfg
    )
    invalid_banner = render_run_status_banner(invalid, cov)
    assert "NOT A TRUSTED RANKING" in invalid_banner


def test_build_and_write_provider_diagnostics(tmp_path):
    cfg = _cfg()
    cov = assess_coverage(_healthy_stages(), _ranked(), cfg)
    status = determine_run_status(cov, None, cfg)
    diag = build_provider_diagnostics(
        status=status, coverage=cov, health_report={"overall_status": "healthy"},
        provider_failures={"total": 0}, fallback_usage={}, cache_usage={},
        providers={"prices": "yfinance", "fundamentals": "yfinance", "news": "yfinance"},
    )
    assert diag["run_status"] == "VALID"
    assert diag["is_trusted"] is True
    assert diag["configured_providers"]["prices"] == "yfinance"

    json_path, md_path = write_provider_diagnostics(diag, tmp_path)
    assert json_path.exists() and md_path.exists()
    assert "# Provider Diagnostics" in md_path.read_text(encoding="utf-8")


# ===========================================================================
# 6. Provider-symbol normalization (class shares)
# ===========================================================================

def test_class_share_normalization_for_dot_hyphen_providers():
    assert to_provider_symbol("BRK.B", "yfinance") == "BRK-B"
    assert to_provider_symbol("BRK.B", "stooq") == "BRK-B"
    assert to_provider_symbol("BF.B", "stooq") == "BF-B"
