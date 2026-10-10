"""Portfolio allocation: config, weighting, caps, money, and the trust gate."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from asset_selection.allocation import (
    PortfolioConfigError,
    build_plan,
    eligible_candidates,
    finalize_plan,
    load_config,
    order_rows,
    raw_weights,
    validate_config,
)
from asset_selection.allocation.config import (
    INSTRUMENT_SLEEVES,
    SLEEVE_EQUITY_CORE,
    SLEEVE_GROWTH_SATELLITE,
    SLEEVE_SPECULATIVE,
)
from asset_selection.pipelines.run_portfolio_allocation import (
    _parse_prices,
    main,
    run_allocation,
)

PRICES = {"SPLG": 68.0, "BND": 73.0}


def _cfg(tmp_path=None, **capital):
    cfg = load_config({})
    for k, v in capital.items():
        setattr(cfg.capital, k, v)
    if tmp_path is not None:
        cfg.run.output_dir = str(tmp_path / "reports")
        cfg.run.processed_dir = str(tmp_path / "processed")
    return cfg


def _ranking(specs=None) -> pd.DataFrame:
    specs = specs or [
        ("AAPL", "core_candidate", "Technology", 78, 0.25, 190.0, "low_risk", True),
        ("MSFT", "core_candidate", "Technology", 76, 0.22, 420.0, "low_risk", True),
        ("JNJ", "core_candidate", "Healthcare", 70, 0.16, 155.0, "low_risk", True),
        ("PG", "core_candidate", "Consumer Defensive", 68, 0.14, 165.0, "low_risk", True),
        ("JPM", "core_candidate", "Financial Services", 66, 0.21, 205.0, "low_risk", True),
        ("KO", "core_candidate", "Consumer Defensive", 65, 0.15, 62.0, "low_risk", True),
        ("XOM", "core_candidate", "Energy", 64, 0.26, 115.0, "moderate_risk", True),
        ("NVDA", "satellite_growth_candidate", "Technology", 82, 0.52, 120.0, "moderate_risk", True),
        ("AMD", "satellite_growth_candidate", "Technology", 72, 0.55, 160.0, "moderate_risk", True),
        ("LLY", "satellite_growth_candidate", "Healthcare", 74, 0.33, 88.0, "moderate_risk", True),
        ("CAT", "satellite_growth_candidate", "Industrials", 63, 0.28, 34.0, "moderate_risk", True),
        ("MOON", "speculative_research_only", "Technology", 60, 1.10, 12.0, "high_risk", False),
        ("ZAPP", "speculative_research_only", "Consumer Cyclical", 55, 1.30, 4.5, "high_risk", False),
    ]
    return pd.DataFrame([
        dict(ticker=t, candidate_role=r, sector=sec, allocation_adjusted_score=sc,
             final_score=sc, volatility_pct=v, last_close=px, risk_bucket=rb,
             eligible_for_allocation=el, rank=i, fundamentals_score=sc - 5,
             sentiment_score=55.0, sentiment_confidence=0.45, risk_penalty=12.0,
             company_name=f"{t} Inc")
        for i, (t, r, sec, sc, v, px, rb, el) in enumerate(specs, 1)
    ])


def _plan(strategy="balanced", df=None, cfg=None, prices=PRICES):
    cfg = cfg or _cfg()
    df = _ranking() if df is None else df
    p = build_plan(df, strategy, cfg.strategies[strategy], cfg,
                   price_lookup=(prices or {}).get)
    return finalize_plan(p, cfg)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_shipped_config_and_defaults_are_valid():
    assert validate_config(load_config({})) == []
    cfg = load_config("configs/us_equities/portfolio.yaml")
    assert validate_config(cfg) == []
    assert set(cfg.strategies) == {"conservative", "balanced", "aggressive"}


def test_every_strategy_budget_sums_to_one():
    for name, s in load_config({}).strategies.items():
        assert sum(s.sleeves.values()) == pytest.approx(1.0), name


@pytest.mark.parametrize("raw,fragment", [
    ({"capital": {"investable_amount": 0}}, "investable_amount must be positive"),
    ({"capital": {"commission_per_trade_pct": 0.5}}, "commission_per_trade_pct"),
    ({"strategies": {"balanced": {"sleeves": {"cash": 0.9}}}}, "must sum to 1.0"),
    ({"strategies": {"balanced": {"max_weight_per_name": 0.5,
                                  "max_weight_per_sector": 0.2}}}, "exceeds"),
    ({"strategies": {"balanced": {"max_names_per_sleeve": 0}}}, "max_names_per_sleeve"),
    ({"strategies": {"balanced": {"inverse_vol_tilt": 2.0}}}, "inverse_vol_tilt"),
    ({"strategies": {"balanced": {"allowed_risk_buckets": ["nope"]}}}, "allowed_risk_buckets"),
    ({"strategies": {"balanced": {"overflow_sleeve": "speculative"}}}, "overflow_sleeve"),
    ({"strategies": {"balanced": {"sleeves": {"nonsense": 0.0}}}}, "unknown sleeves"),
])
def test_invalid_configs_rejected(raw, fragment):
    with pytest.raises(PortfolioConfigError) as exc:
        load_config(raw)
    assert fragment in str(exc.value)


def test_speculative_budget_requires_opt_in():
    with pytest.raises(PortfolioConfigError) as exc:
        load_config({"strategies": {"conservative": {
            "sleeves": {"cash": 0.10, "bond_proxy": 0.35, "index_core": 0.35,
                        "equity_core": 0.15, "growth_satellite": 0.0,
                        "speculative": 0.05},
            "allow_speculative": False}}})
    assert "allow_speculative" in str(exc.value)


def test_infeasible_sleeve_budget_rejected():
    # 30% sleeve cannot fit in 2 names capped at 6% each.
    with pytest.raises(PortfolioConfigError) as exc:
        load_config({"strategies": {"balanced": {"max_names_per_sleeve": 2}}})
    assert "exceeds what" in str(exc.value)


# ---------------------------------------------------------------------------
# Eligibility and sleeve assignment
# ---------------------------------------------------------------------------

def test_speculative_names_excluded_unless_opted_in():
    cfg = _cfg()
    df = _ranking()
    conservative = eligible_candidates(df, cfg.strategies["conservative"])
    assert "MOON" not in set(conservative["ticker"])
    aggressive = eligible_candidates(df, cfg.strategies["aggressive"])
    assert {"MOON", "ZAPP"} <= set(aggressive["ticker"])


def test_risk_bucket_filter_applies_to_equity_sleeves():
    cfg = _cfg()
    conservative = eligible_candidates(_ranking(), cfg.strategies["conservative"])
    # conservative allows only low_risk: XOM (moderate) must be out
    assert "XOM" not in set(conservative["ticker"])
    assert "AAPL" in set(conservative["ticker"])


def test_names_without_price_are_not_allocated():
    df = _ranking()
    df.loc[df["ticker"] == "AAPL", "last_close"] = 0
    kept = eligible_candidates(df, _cfg().strategies["balanced"])
    assert "AAPL" not in set(kept["ticker"])


def test_positions_land_in_the_sleeve_matching_their_role():
    plan = _plan("aggressive")
    by_ticker = {p.ticker: p for p in plan.positions}
    assert by_ticker["AAPL"].sleeve == SLEEVE_EQUITY_CORE
    assert by_ticker["NVDA"].sleeve == SLEEVE_GROWTH_SATELLITE
    assert by_ticker["MOON"].sleeve == SLEEVE_SPECULATIVE
    assert "SPECULATIVE_RESEARCH_ONLY" in by_ticker["MOON"].warnings


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------

def test_raw_weights_sum_to_one_and_follow_score_at_zero_tilt():
    rows = [
        {"allocation_adjusted_score": 80, "volatility_pct": 0.5},
        {"allocation_adjusted_score": 40, "volatility_pct": 0.2},
    ]
    w = raw_weights(rows, tilt=0.0)
    assert sum(w) == pytest.approx(1.0)
    assert w[0] / w[1] == pytest.approx(2.0)          # pure score: 80/40


def test_pure_inverse_vol_tilt_ignores_score():
    rows = [
        {"allocation_adjusted_score": 80, "volatility_pct": 0.4},
        {"allocation_adjusted_score": 40, "volatility_pct": 0.2},
    ]
    w = raw_weights(rows, tilt=1.0)
    assert w[1] / w[0] == pytest.approx(2.0)          # half the vol, twice the weight


def test_missing_volatility_falls_back_to_the_sleeve_median():
    rows = [
        {"allocation_adjusted_score": 50, "volatility_pct": 0.2},
        {"allocation_adjusted_score": 50, "volatility_pct": None},
        {"allocation_adjusted_score": 50, "volatility_pct": 0.2},
    ]
    w = raw_weights(rows, tilt=1.0)
    assert sum(w) == pytest.approx(1.0)
    assert w[1] == pytest.approx(w[0])                # median == 0.2


def test_sleeve_budgets_are_respected():
    cfg = _cfg()
    plan = _plan("balanced", cfg=cfg)
    for sleeve in (SLEEVE_EQUITY_CORE, SLEEVE_GROWTH_SATELLITE, SLEEVE_SPECULATIVE):
        res = next(s for s in plan.sleeves if s.sleeve == sleeve)
        allocated = sum(p.target_weight for p in plan.positions if p.sleeve == sleeve)
        assert allocated == pytest.approx(res.target_budget, abs=1e-6)


def test_total_target_weight_is_always_one():
    for strategy in ("conservative", "balanced", "aggressive"):
        plan = _plan(strategy)
        total = sum(p.target_weight for p in plan.positions)
        cash = next(s for s in plan.sleeves if s.sleeve == "cash").target_budget
        assert total + cash == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Concentration caps
# ---------------------------------------------------------------------------

def test_per_name_cap_is_never_exceeded():
    for strategy in ("conservative", "balanced", "aggressive"):
        cfg = _cfg()
        plan = _plan(strategy, cfg=cfg)
        cap = cfg.strategies[strategy].max_weight_per_name
        for p in plan.positions:
            if p.sleeve in INSTRUMENT_SLEEVES:
                continue                     # broad instruments are exempt
            assert p.target_weight <= cap + 1e-6, (strategy, p.ticker)


def test_per_sector_cap_is_never_exceeded_for_single_names():
    # A tech-heavy universe must still respect the sector cap.
    tech = [
        (f"T{i}", "core_candidate", "Technology", 70 + i, 0.3, 50.0, "low_risk", True)
        for i in range(8)
    ]
    cfg = _cfg()
    plan = _plan("balanced", df=_ranking(tech), cfg=cfg)
    cap = cfg.strategies["balanced"].max_weight_per_sector
    by_sector = {}
    for p in plan.positions:
        if p.sleeve in INSTRUMENT_SLEEVES:
            continue
        by_sector[p.sector] = by_sector.get(p.sector, 0.0) + p.target_weight
    for sector, w in by_sector.items():
        assert w <= cap + 1e-6, (sector, w)


def test_sector_overflow_goes_to_the_index_core_and_is_reported():
    # Tech spans the core AND growth sleeves (25% + 13% = 38% of the
    # portfolio), which must be cut back to the 25% per-sector cap.
    tech = [
        (f"C{i}", "core_candidate", "Technology", 70, 0.3, 50.0, "low_risk", True)
        for i in range(8)
    ] + [
        (f"G{i}", "satellite_growth_candidate", "Technology", 68, 0.4, 40.0,
         "moderate_risk", True)
        for i in range(8)
    ]
    cfg = _cfg()
    plan = _plan("balanced", df=_ranking(tech), cfg=cfg)
    index = next(p for p in plan.positions if p.sleeve == "index_core")
    budget = cfg.strategies["balanced"].sleeves["index_core"]
    assert index.target_weight > budget            # absorbed the overflow
    assert "SECTOR_CAP_OVERFLOW" in plan.warnings
    assert sum(p.target_weight for p in plan.positions) == pytest.approx(
        1.0 - cfg.strategies["balanced"].sleeves["cash"], abs=1e-6
    )


def test_empty_sleeve_budget_overflows_and_warns():
    # Only core names exist -> the growth and speculative sleeves are empty.
    core_only = [
        ("AAPL", "core_candidate", "Technology", 78, 0.25, 190.0, "low_risk", True),
        ("JNJ", "core_candidate", "Healthcare", 70, 0.16, 155.0, "low_risk", True),
        ("PG", "core_candidate", "Consumer Defensive", 68, 0.14, 165.0, "low_risk", True),
        ("JPM", "core_candidate", "Financial Services", 66, 0.21, 205.0, "low_risk", True),
        ("KO", "core_candidate", "Consumer Defensive", 65, 0.15, 62.0, "low_risk", True),
    ]
    plan = _plan("aggressive", df=_ranking(core_only))
    assert "EMPTY_SLEEVE:growth_satellite" in plan.warnings
    assert "EMPTY_SLEEVE:speculative" in plan.warnings
    growth = next(s for s in plan.sleeves if s.sleeve == SLEEVE_GROWTH_SATELLITE)
    assert growth.overflow_weight == pytest.approx(growth.target_budget)
    assert sum(p.target_weight for p in plan.positions) == pytest.approx(0.97, abs=1e-6)


def test_disabled_speculative_sleeve_redirects_its_budget():
    cfg = _cfg()
    cfg.strategies["aggressive"].allow_speculative = False
    plan = _plan("aggressive", cfg=cfg)
    assert not [p for p in plan.positions if p.sleeve == SLEEVE_SPECULATIVE]
    index = next(p for p in plan.positions if p.sleeve == "index_core")
    assert index.target_weight > cfg.strategies["aggressive"].sleeves["index_core"]


# ---------------------------------------------------------------------------
# Money layer
# ---------------------------------------------------------------------------

def test_plan_never_overspends_the_capital():
    for strategy in ("conservative", "balanced", "aggressive"):
        cfg = _cfg(investable_amount=25_000.0)
        plan = _plan(strategy, cfg=cfg)
        spent = plan.invested_value + plan.commission_total
        assert spent <= cfg.capital.investable_amount + 1e-6
        assert plan.cash_value >= 0
        # Money is conserved: invested + commission + cash == capital.
        assert spent + plan.cash_value == pytest.approx(
            cfg.capital.investable_amount, abs=0.01
        )


def test_whole_shares_by_default_and_fractional_when_enabled():
    plan = _plan("balanced", cfg=_cfg(investable_amount=25_000.0))
    assert all(float(p.shares).is_integer() for p in plan.positions)

    cfg = _cfg(investable_amount=25_000.0, allow_fractional_shares=True)
    frac = _plan("balanced", cfg=cfg)
    assert any(not float(p.shares).is_integer() for p in frac.positions)
    # Fractional shares place the money much more precisely.
    assert frac.invested_value > plan.invested_value


def test_commission_is_charged_per_position():
    cfg = _cfg(investable_amount=25_000.0, commission_per_trade_pct=0.002)
    plan = _plan("balanced", cfg=cfg)
    for p in plan.positions:
        if p.invested_value > 0:
            assert p.commission == pytest.approx(p.invested_value * 0.002)
    assert plan.commission_total == pytest.approx(
        sum(p.commission for p in plan.positions)
    )


def test_targets_below_the_minimum_are_reported_not_silently_zeroed():
    # $600 across many sleeves makes most targets tiny.
    cfg = _cfg(investable_amount=600.0, min_position_value=50.0)
    plan = _plan("aggressive", cfg=cfg)
    dropped = [p for p in plan.positions if "BELOW_MIN_POSITION_VALUE" in p.warnings]
    assert dropped
    assert "POSITIONS_NOT_INVESTABLE" in plan.warnings
    assert any("stays in cash" in n for n in plan.notes)


def test_unaffordable_share_price_is_flagged():
    pricey = [
        ("BRK.A", "core_candidate", "Financial Services", 70, 0.2, 700_000.0,
         "low_risk", True),
    ]
    cfg = _cfg(investable_amount=10_000.0)
    plan = _plan("balanced", df=_ranking(pricey), cfg=cfg)
    brk = next(p for p in plan.positions if p.ticker == "BRK.A")
    assert brk.shares == 0
    assert "PRICE_EXCEEDS_TARGET_VALUE" in brk.warnings


def test_missing_instrument_price_yields_dollar_target_without_shares():
    plan = _plan("balanced", prices={})           # no ETF prices available
    splg = next(p for p in plan.positions if p.ticker == "SPLG")
    assert splg.target_value > 0 and splg.shares == 0
    assert "PRICE_UNAVAILABLE_NO_SHARE_COUNT" in splg.warnings
    assert any(w.startswith("PRICE_UNAVAILABLE:") for w in plan.warnings)


def test_order_ticket_only_lists_executable_buys():
    plan = _plan("balanced", cfg=_cfg(investable_amount=25_000.0))
    orders = order_rows(plan)
    assert orders and all(o["shares"] > 0 for o in orders)
    assert all(o["action"] == "BUY" for o in orders)
    assert orders == sorted(orders, key=lambda o: -float(o["weight_pct"]))


# ---------------------------------------------------------------------------
# Diversification and risk laddering
# ---------------------------------------------------------------------------

def test_risk_increases_monotonically_across_the_three_strategies():
    cfg = _cfg(investable_amount=25_000.0)
    plans = {s: _plan(s, cfg=cfg) for s in ("conservative", "balanced", "aggressive")}
    equity = [plans[s].equity_weight for s in ("conservative", "balanced", "aggressive")]
    assert equity[0] < equity[1] < equity[2]

    def sleeve(p, name):
        return next(s for s in p.sleeves if s.sleeve == name).allocated_weight

    spec = [sleeve(plans[s], "speculative")
            for s in ("conservative", "balanced", "aggressive")]
    assert spec == sorted(spec) and spec[0] == 0.0 and spec[2] > spec[1]
    stability = [
        sleeve(plans[s], "cash") + sleeve(plans[s], "bond_proxy")
        for s in ("conservative", "balanced", "aggressive")
    ]
    assert stability[0] > stability[1] > stability[2]


def test_effective_positions_is_bounded_by_the_holding_count():
    plan = _plan("aggressive", cfg=_cfg(investable_amount=25_000.0))
    held = sum(1 for p in plan.positions if p.invested_value > 0)
    assert 1.0 <= plan.effective_positions <= held + 1e-9


def test_sector_weights_cover_the_invested_book():
    plan = _plan("balanced", cfg=_cfg(investable_amount=25_000.0))
    assert sum(plan.sector_weights.values()) == pytest.approx(
        sum(p.realized_weight for p in plan.positions if p.invested_value > 0),
        abs=1e-9,
    )
    assert len(plan.sector_weights) >= 4          # several business types


def test_aggressive_holds_more_business_types_than_conservative():
    cfg = _cfg(investable_amount=25_000.0)
    cons = _plan("conservative", cfg=cfg)
    aggr = _plan("aggressive", cfg=cfg)
    assert len(aggr.sector_weights) >= len(cons.sector_weights)


# ---------------------------------------------------------------------------
# Pipeline, trust gate and CLI
# ---------------------------------------------------------------------------

def test_untrusted_selection_run_is_refused():
    cfg = _cfg()
    result = run_allocation(
        cfg, df=_ranking(),
        provenance={"run_id": "x", "run_status": "INVALID",
                    "ranking_validity": "INVALID_PROVIDER_FAILURE"},
        price_lookup=PRICES.get,
    )
    assert result.status == "INVALID" and not result.plans
    assert any("not trustworthy" in r for r in result.status_reasons)


def test_untrusted_run_can_be_overridden_explicitly():
    cfg = _cfg()
    cfg.run.require_trusted_run = False
    result = run_allocation(
        cfg, df=_ranking(),
        provenance={"run_id": "x", "run_status": "INVALID", "ranking_validity": "x"},
        price_lookup=PRICES.get,
    )
    assert result.status == "DEGRADED" and result.plans


def test_missing_selection_verdict_degrades_but_still_plans():
    result = run_allocation(
        _cfg(), df=_ranking(),
        provenance={"run_id": None, "run_status": None, "ranking_validity": None},
        price_lookup=PRICES.get,
    )
    assert result.status == "DEGRADED"
    assert any("unverified" in r for r in result.status_reasons)
    assert len(result.plans) == 3


def test_partial_selection_run_propagates():
    result = run_allocation(
        _cfg(), df=_ranking(),
        provenance={"run_id": "x", "run_status": "PARTIAL", "ranking_validity": "P"},
        price_lookup=PRICES.get,
    )
    assert result.status == "PARTIAL"
    assert result.plans


def test_empty_ranking_is_invalid():
    result = run_allocation(
        _cfg(), df=_ranking().iloc[0:0],
        provenance={"run_id": "x", "run_status": "VALID", "ranking_validity": "V"},
        price_lookup=PRICES.get,
    )
    assert result.status == "INVALID" and not result.plans


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="Unknown strategy"):
        run_allocation(
            _cfg(), df=_ranking(),
            provenance={"run_id": "x", "run_status": "VALID", "ranking_validity": "V"},
            strategies=["moonshot"], price_lookup=PRICES.get,
        )


def test_outputs_are_written(tmp_path):
    cfg = _cfg(tmp_path, investable_amount=25_000.0)
    from asset_selection.allocation.reporting import write_outputs

    result = run_allocation(
        cfg, df=_ranking(),
        provenance={"run_id": "sel-1", "run_status": "VALID",
                    "ranking_validity": "VALID_RANKING"},
        price_lookup=PRICES.get,
    )
    paths = write_outputs(result, cfg)
    for key in ("combined_csv", "summary_json", "report_md",
                "conservative_csv", "balanced_csv", "aggressive_csv"):
        assert Path(paths[key]).exists(), key

    df = pd.read_csv(paths["combined_csv"])
    assert {"strategy", "ticker", "sleeve", "target_weight_pct",
            "realized_weight_pct", "invested_value", "shares"} <= set(df.columns)
    assert set(df["strategy"]) == {"conservative", "balanced", "aggressive"}

    summary = json.loads(Path(paths["summary_json"]).read_text())
    assert summary["source"]["run_status"] == "VALID"
    assert set(summary["strategies"]) == {"conservative", "balanced", "aggressive"}

    md = Path(paths["report_md"]).read_text()
    assert "not financial advice" in md
    assert "Strategy comparison" in md
    assert "Order ticket" in md
    assert "Diversification by business sector" in md
    assert "Positions — percentage and money" in md


def test_cli_parses_instrument_prices():
    assert _parse_prices(["SPLG=68.10", "bnd=73.4"]) == {"SPLG": 68.1, "BND": 73.4}
    for bad in (["SPLG"], ["SPLG=abc"], ["SPLG=-1"]):
        with pytest.raises(ValueError):
            _parse_prices(bad)


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    processed = tmp_path / "data" / "us_equities" / "processed"
    processed.mkdir(parents=True)
    reports = tmp_path / "reports" / "us_equities"
    reports.mkdir(parents=True)
    _ranking().to_csv(processed / "asset_selection_results.csv", index=False)
    (reports / "asset_selection_summary.json").write_text(json.dumps(
        {"run_id": "sel-1", "run_status": "VALID", "ranking_validity": "VALID_RANKING"}
    ))
    monkeypatch.chdir(tmp_path)

    rc = main([
        "--config", str(Path(__file__).resolve().parents[2]
                        / "configs" / "us_equities" / "portfolio.yaml"),
        "--amount", "25000", "--offline",
        "--instrument-price", "SPLG=68.0", "BND=73.0",
        "--log-level", "ERROR",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "conservative" in out and "aggressive" in out
    assert (tmp_path / "reports" / "us_equities" / "portfolio"
            / "portfolio_allocation.md").exists()


def test_cli_refuses_a_missing_ranking(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["--input", str(tmp_path / "nope.csv"), "--log-level", "ERROR"])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_cli_no_speculative_keeps_budget_balanced(tmp_path, monkeypatch):
    processed = tmp_path / "data" / "us_equities" / "processed"
    processed.mkdir(parents=True)
    _ranking().to_csv(processed / "asset_selection_results.csv", index=False)
    monkeypatch.chdir(tmp_path)
    rc = main([
        "--amount", "25000", "--offline", "--no-speculative",
        "--allow-untrusted-ranking",
        "--instrument-price", "SPLG=68.0", "BND=73.0", "--log-level", "ERROR",
    ])
    assert rc == 0
    df = pd.read_csv(
        tmp_path / "data" / "us_equities" / "processed" / "portfolio_allocations.csv"
    )
    assert "speculative" not in set(df["sleeve"])
