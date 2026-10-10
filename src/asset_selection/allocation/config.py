"""Typed configuration for portfolio allocation.

Three risk strategies (conservative / balanced / aggressive), each a **sleeve
budget** over the asset-class buckets a core-satellite portfolio uses. The
equity sleeves are *filled from the selection ranking* rather than hard-coded
tickers — that is the link between asset selection and allocation.

Validation is strict and reports every problem at once (same contract as
``binary_options_selection.config``): budgets must sum to 1, caps must be
feasible against the sleeve they constrain, and a strategy may not enable a
speculative budget without explicitly opting in.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

_DEFAULT_CONFIG_PATH = Path("configs/us_equities/portfolio.yaml")

# ---------------------------------------------------------------------------
# Sleeves
# ---------------------------------------------------------------------------

SLEEVE_CASH = "cash"
SLEEVE_BOND_PROXY = "bond_proxy"
SLEEVE_INDEX_CORE = "index_core"
SLEEVE_EQUITY_CORE = "equity_core"
SLEEVE_GROWTH_SATELLITE = "growth_satellite"
SLEEVE_SPECULATIVE = "speculative"

# Ordered for reporting (stable, lowest risk first).
SLEEVES = (
    SLEEVE_CASH,
    SLEEVE_BOND_PROXY,
    SLEEVE_INDEX_CORE,
    SLEEVE_EQUITY_CORE,
    SLEEVE_GROWTH_SATELLITE,
    SLEEVE_SPECULATIVE,
)

# Sleeves whose constituents come from the selection ranking (single names).
SELECTION_FILLED_SLEEVES = (
    SLEEVE_EQUITY_CORE,
    SLEEVE_GROWTH_SATELLITE,
    SLEEVE_SPECULATIVE,
)

# Sleeves held through one instrument each (an ETF / cash).
INSTRUMENT_SLEEVES = (SLEEVE_CASH, SLEEVE_BOND_PROXY, SLEEVE_INDEX_CORE)

# selection `candidate_role` -> sleeve it may fill.
ROLE_TO_SLEEVE = {
    "core_candidate": SLEEVE_EQUITY_CORE,
    "satellite_growth_candidate": SLEEVE_GROWTH_SATELLITE,
    "speculative_research_only": SLEEVE_SPECULATIVE,
}

# Sector label used for broad-index and bond holdings, which are diversified
# by construction and therefore exempt from the single-sector cap.
BROAD_SECTOR = "BROAD_MARKET"

STRATEGY_NAMES = ("conservative", "balanced", "aggressive")


class PortfolioConfigError(ValueError):
    """Raised when the allocation configuration is semantically invalid."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"Invalid portfolio configuration:\n  - {joined}")


# ---------------------------------------------------------------------------
# Dataclass tree
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    # Selection output consumed as the candidate universe.
    input_csv: str = "data/us_equities/processed/asset_selection_results.csv"
    # Sibling summary that carries the selection run's own verdict
    # (run_status / ranking_validity). Used as the trust gate.
    input_summary_json: str = "reports/us_equities/asset_selection_summary.json"
    output_dir: str = "reports/us_equities/portfolio"
    processed_dir: str = "data/us_equities/processed"
    # Refuse to build a plan on a ranking the selection run did not trust
    # (run_status DIAGNOSTIC / INVALID).
    require_trusted_run: bool = True


@dataclass
class CapitalConfig:
    """How much money is being allocated, and the frictions of placing it."""
    investable_amount: float = 10_000.0
    currency: str = "USD"
    # Per-order commission as a fraction of notional (0.002 = 0.20%).
    commission_per_trade_pct: float = 0.002
    # Whole shares by default: a plan you could actually execute.
    allow_fractional_shares: bool = False
    # Positions whose target value is below this are dropped as uninvestable
    # (their budget is redistributed) instead of producing 0-share rows.
    min_position_value: float = 50.0


@dataclass
class InstrumentsConfig:
    """Tickers used for the sleeves that are held through one instrument.

    These are broad, liquid, well-known ETFs; they are configuration, not
    recommendations. Prices are fetched from the configured price provider —
    never invented.
    """
    index_core_ticker: str = "SPLG"      # S&P 500 core (low-fee share class)
    index_core_name: str = "S&P 500 index ETF"
    bond_proxy_ticker: str = "BND"       # total US bond market
    bond_proxy_name: str = "Aggregate bond ETF"


@dataclass
class StrategyConfig:
    """One risk strategy: a sleeve budget plus its concentration limits."""
    label: str = ""
    description: str = ""
    # Sleeve budgets as fractions of the total portfolio; must sum to 1.
    sleeves: Dict[str, float] = field(default_factory=dict)
    # Hard concentration limits, as fractions of the TOTAL portfolio.
    max_weight_per_name: float = 0.06
    max_weight_per_sector: float = 0.25
    # Cap on how many single names each selection-filled sleeve may hold.
    max_names_per_sleeve: int = 8
    # Blend between score-weighting and inverse-volatility weighting:
    # 0.0 = pure score weight, 1.0 = pure inverse-vol (risk-parity-lite).
    inverse_vol_tilt: float = 0.5
    # Risk buckets a strategy will accept into its equity sleeves.
    allowed_risk_buckets: List[str] = field(
        default_factory=lambda: ["low_risk", "moderate_risk"]
    )
    # The speculative sleeve requires an explicit opt-in, because selection
    # marks those names `exclude_from_allocation_by_default`.
    allow_speculative: bool = False
    # Where an unfillable sleeve budget goes (no eligible names, or capped
    # out). Must be an instrument sleeve so it is always placeable.
    overflow_sleeve: str = SLEEVE_INDEX_CORE


@dataclass
class PortfolioConfig:
    run: RunConfig = field(default_factory=RunConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)
    instruments: InstrumentsConfig = field(default_factory=InstrumentsConfig)
    strategies: Dict[str, StrategyConfig] = field(
        default_factory=lambda: _default_strategies()
    )


def _default_strategies() -> Dict[str, StrategyConfig]:
    """Sleeve budgets modeled on advisory risk profiles (core-satellite).

    Conservative keeps most capital in cash + bonds + a broad index and holds
    only a few high-quality single names. Balanced is a classic core-satellite
    split. Aggressive tilts to growth satellites and opens a small, hard-capped
    speculative sleeve. Every budget sums to 1.0.
    """
    return {
        "conservative": StrategyConfig(
            label="Conservative — capital preservation",
            description=(
                "Low risk. Most of the portfolio sits in cash, bonds and a "
                "broad S&P 500 index; single names are few, large and stable. "
                "No speculative sleeve."
            ),
            sleeves={
                SLEEVE_CASH: 0.10,
                SLEEVE_BOND_PROXY: 0.40,
                SLEEVE_INDEX_CORE: 0.35,
                SLEEVE_EQUITY_CORE: 0.15,
                SLEEVE_GROWTH_SATELLITE: 0.00,
                SLEEVE_SPECULATIVE: 0.00,
            },
            max_weight_per_name=0.04,
            max_weight_per_sector=0.15,
            max_names_per_sleeve=6,
            inverse_vol_tilt=0.70,
            allowed_risk_buckets=["low_risk"],
            allow_speculative=False,
        ),
        "balanced": StrategyConfig(
            label="Balanced — core and satellite",
            description=(
                "Medium risk. A broad index plus quality single names form the "
                "core; growth satellites take small weights; a token "
                "speculative sleeve is allowed only when opted in."
            ),
            sleeves={
                SLEEVE_CASH: 0.05,
                SLEEVE_BOND_PROXY: 0.20,
                SLEEVE_INDEX_CORE: 0.35,
                SLEEVE_EQUITY_CORE: 0.25,
                SLEEVE_GROWTH_SATELLITE: 0.13,
                SLEEVE_SPECULATIVE: 0.02,
            },
            max_weight_per_name=0.06,
            max_weight_per_sector=0.25,
            max_names_per_sleeve=8,
            inverse_vol_tilt=0.50,
            allowed_risk_buckets=["low_risk", "moderate_risk"],
            allow_speculative=True,
        ),
        "aggressive": StrategyConfig(
            label="Aggressive — growth tilted",
            description=(
                "High risk. Smaller stability sleeves, a larger growth "
                "satellite allocation and a hard-capped speculative sleeve "
                "for asymmetric outcomes. Expect materially larger drawdowns."
            ),
            sleeves={
                SLEEVE_CASH: 0.03,
                SLEEVE_BOND_PROXY: 0.05,
                SLEEVE_INDEX_CORE: 0.27,
                SLEEVE_EQUITY_CORE: 0.30,
                SLEEVE_GROWTH_SATELLITE: 0.25,
                SLEEVE_SPECULATIVE: 0.10,
            },
            max_weight_per_name=0.10,
            max_weight_per_sector=0.35,
            max_names_per_sleeve=10,
            inverse_vol_tilt=0.25,
            allowed_risk_buckets=["low_risk", "moderate_risk", "high_risk"],
            allow_speculative=True,
        ),
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_config(
    source: Union[str, Path, Dict[str, Any], None] = None,
    *,
    validate: bool = True,
) -> PortfolioConfig:
    """Load from a path, a dict, or the default on-disk config.

    Raises :class:`PortfolioConfigError` listing every problem unless
    ``validate=False``.
    """
    raw: Dict[str, Any]
    if isinstance(source, dict):
        raw = source
    else:
        path = _resolve_path(source)
        if isinstance(source, (str, Path)) and (path is None or not path.exists()):
            raise FileNotFoundError(f"Portfolio config not found: {source}")
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}

    cfg = _from_dict(raw)
    if validate:
        problems = validate_config(cfg)
        if problems:
            raise PortfolioConfigError(problems)
    return cfg


def _resolve_path(source: Union[str, Path, None]) -> Optional[Path]:
    if isinstance(source, (str, Path)):
        return Path(source)
    env = os.environ.get("PORTFOLIO_CONFIG")
    if env:
        return Path(env)
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH
    return None


def _from_dict(raw: Dict[str, Any]) -> PortfolioConfig:
    def section(name: str) -> Dict[str, Any]:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    strategies = _default_strategies()
    for name, block in (section("strategies") or {}).items():
        if not isinstance(block, dict):
            continue
        base = strategies.get(name) or StrategyConfig()
        merged = {f.name: getattr(base, f.name) for f in fields(StrategyConfig)}
        for key, value in block.items():
            if key in merged:
                merged[key] = value
        # Sleeve budgets merge onto the base so a YAML may override one sleeve.
        if isinstance(block.get("sleeves"), dict):
            sleeves = dict(base.sleeves)
            sleeves.update({str(k): v for k, v in block["sleeves"].items()})
            merged["sleeves"] = sleeves
        strategies[name] = StrategyConfig(**merged)

    return PortfolioConfig(
        run=RunConfig(**_filtered(RunConfig, section("run"))),
        capital=CapitalConfig(**_filtered(CapitalConfig, section("capital"))),
        instruments=InstrumentsConfig(**_filtered(InstrumentsConfig, section("instruments"))),
        strategies=strategies,
    )


def _filtered(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in valid}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_BUDGET_TOLERANCE = 1e-6
VALID_RISK_BUCKETS = ("low_risk", "moderate_risk", "high_risk")


def validate_config(cfg: PortfolioConfig) -> List[str]:
    """Return every problem found (empty list = valid)."""
    problems: List[str] = []

    cap = cfg.capital
    if cap.investable_amount <= 0:
        problems.append("capital.investable_amount must be positive")
    if not (0.0 <= cap.commission_per_trade_pct < 0.1):
        problems.append(
            "capital.commission_per_trade_pct must be within [0, 0.1) "
            f"(got {cap.commission_per_trade_pct})"
        )
    if cap.min_position_value < 0:
        problems.append("capital.min_position_value must be >= 0")

    if not cfg.instruments.index_core_ticker:
        problems.append("instruments.index_core_ticker must not be empty")
    if not cfg.instruments.bond_proxy_ticker:
        problems.append("instruments.bond_proxy_ticker must not be empty")

    if not cfg.strategies:
        problems.append("at least one strategy must be configured")

    for name, s in cfg.strategies.items():
        problems.extend(_validate_strategy(name, s))

    return problems


def _validate_strategy(name: str, s: StrategyConfig) -> List[str]:
    problems: List[str] = []
    prefix = f"strategies.{name}"

    unknown = set(s.sleeves) - set(SLEEVES)
    if unknown:
        problems.append(
            f"{prefix}.sleeves has unknown sleeves {sorted(unknown)}; "
            f"allowed: {list(SLEEVES)}"
        )

    numeric: Dict[str, float] = {}
    for sleeve, weight in s.sleeves.items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            problems.append(f"{prefix}.sleeves.{sleeve} {weight!r} is not a number")
            continue
        if w < 0:
            problems.append(f"{prefix}.sleeves.{sleeve} must be >= 0 (got {w})")
        numeric[sleeve] = w

    total = sum(numeric.values())
    if numeric and abs(total - 1.0) > 1e-4:
        problems.append(
            f"{prefix}.sleeves must sum to 1.0 (got {total:.4f}) — the budget "
            "is the whole portfolio including cash"
        )

    if numeric.get(SLEEVE_SPECULATIVE, 0.0) > 0 and not s.allow_speculative:
        problems.append(
            f"{prefix} has a speculative budget "
            f"({numeric[SLEEVE_SPECULATIVE]:.0%}) but allow_speculative is "
            "false; speculative names are excluded from allocation by default "
            "and need an explicit opt-in"
        )

    if not (0.0 < s.max_weight_per_name <= 1.0):
        problems.append(f"{prefix}.max_weight_per_name must be within (0, 1]")
    if not (0.0 < s.max_weight_per_sector <= 1.0):
        problems.append(f"{prefix}.max_weight_per_sector must be within (0, 1]")
    if s.max_weight_per_name > s.max_weight_per_sector:
        problems.append(
            f"{prefix}.max_weight_per_name ({s.max_weight_per_name:.0%}) exceeds "
            f"max_weight_per_sector ({s.max_weight_per_sector:.0%}) — a single "
            "name could never satisfy the sector cap"
        )
    if s.max_names_per_sleeve < 1:
        problems.append(f"{prefix}.max_names_per_sleeve must be >= 1")
    if not (0.0 <= s.inverse_vol_tilt <= 1.0):
        problems.append(f"{prefix}.inverse_vol_tilt must be within [0, 1]")

    # Feasibility: a sleeve's budget must be reachable given the per-name cap
    # and how many names it may hold, otherwise it silently overflows forever.
    for sleeve in SELECTION_FILLED_SLEEVES:
        budget = numeric.get(sleeve, 0.0)
        reachable = s.max_weight_per_name * s.max_names_per_sleeve
        if budget > reachable + _BUDGET_TOLERANCE:
            problems.append(
                f"{prefix}.sleeves.{sleeve} budget {budget:.0%} exceeds what "
                f"{s.max_names_per_sleeve} names capped at "
                f"{s.max_weight_per_name:.0%} can hold ({reachable:.0%})"
            )

    bad_buckets = set(s.allowed_risk_buckets) - set(VALID_RISK_BUCKETS)
    if bad_buckets:
        problems.append(
            f"{prefix}.allowed_risk_buckets has unknown values "
            f"{sorted(bad_buckets)}; allowed: {list(VALID_RISK_BUCKETS)}"
        )

    if s.overflow_sleeve not in INSTRUMENT_SLEEVES:
        problems.append(
            f"{prefix}.overflow_sleeve must be one of {list(INSTRUMENT_SLEEVES)} "
            "so an unfillable budget is always placeable"
        )

    return problems
