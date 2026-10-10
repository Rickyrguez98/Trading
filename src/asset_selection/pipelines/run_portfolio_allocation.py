"""Portfolio allocation pipeline (Milestone 2).

Consumes the asset-selection ranking and produces three risk strategies with
weights, dollar amounts and share counts.

Stages:
    1. Load config + the selection ranking (and its own run verdict).
    2. Trust gate: refuse to plan on a ranking selection did not trust.
    3. Resolve instrument prices (index core / bond proxy) from the price
       provider — never invented; a missing price yields a dollar target with
       no share count and a visible warning.
    4. Build each strategy: sleeve budgets -> within-sleeve weighting -> caps.
    5. Money layer: whole shares, commissions, residual cash.
    6. Outputs: CSV per strategy + combined, Markdown report, JSON summary.

Outputs (domain-separated):
    data/us_equities/processed/portfolio_{strategy}.csv
    data/us_equities/processed/portfolio_allocations.csv
    reports/us_equities/portfolio/portfolio_allocation.md
    reports/us_equities/portfolio/portfolio_summary.json

Research output only: no orders are placed and nothing here is advice.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from ..allocation.config import (
    PortfolioConfig,
    PortfolioConfigError,
    load_config,
    validate_config,
)
from ..allocation.engine import build_plan, eligible_candidates
from ..allocation.models import AllocationResult
from ..allocation.money import finalize_plan
from ..allocation.reporting import write_outputs
from ..logging_config import configure_logging

logger = logging.getLogger(__name__)

TRUSTED_RUN_STATUSES = ("VALID", "PARTIAL")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_ranking(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Selection ranking not found: {p}. Run the asset-selection "
            "pipeline first (trading-us-equities)."
        )
    df = pd.read_csv(p)
    if "ticker" not in df.columns:
        raise ValueError(f"{p} has no 'ticker' column; not a selection output.")
    return df


def load_provenance(path: str) -> Dict[str, Optional[str]]:
    """Read the selection run's own verdict, when the summary is available."""
    p = Path(path)
    if not p.exists():
        return {"run_id": None, "run_status": None, "ranking_validity": None}
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read selection summary %s: %s", p, exc)
        return {"run_id": None, "run_status": None, "ranking_validity": None}
    return {
        "run_id": blob.get("run_id") or blob.get("generated_at"),
        "run_status": blob.get("run_status"),
        "ranking_validity": blob.get("ranking_validity"),
    }


def build_price_lookup(
    cfg: PortfolioConfig,
    *,
    offline: bool = False,
    overrides: Optional[Dict[str, float]] = None,
) -> "callable":
    """Prices for the instrument sleeves, from the real price provider.

    ``overrides`` are user-supplied prices (``--instrument-price SPLG=68.10``),
    which win over any lookup — useful offline, and honest because the source
    is the user rather than a fabricated value. Returns a lookup that yields
    ``None`` when no price can be obtained: the plan then reports a dollar
    target with no share count rather than inventing one.
    """
    cache: Dict[str, Optional[float]] = dict(overrides or {})
    if offline:
        return lambda ticker: cache.get(ticker)

    def lookup(ticker: str) -> Optional[float]:
        if ticker in cache:
            return cache[ticker]
        price: Optional[float] = None
        try:
            from ..data_providers import build_prices_provider

            provider = build_prices_provider("yfinance")
            snap = provider.fetch(ticker, lookback_days=10)
            value = getattr(snap, "last_close", None)
            price = float(value) if value else None
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            logger.warning("Price lookup failed for %s: %s", ticker, exc)
            price = None
        cache[ticker] = price
        return price

    return lookup


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_allocation(
    cfg: PortfolioConfig,
    *,
    df: Optional[pd.DataFrame] = None,
    provenance: Optional[Dict[str, Optional[str]]] = None,
    price_lookup=None,
    strategies: Optional[List[str]] = None,
    now: Optional[datetime] = None,
) -> AllocationResult:
    """Build every configured strategy. All inputs injectable for testing."""
    now = now or datetime.now(timezone.utc)
    ranking = df if df is not None else load_ranking(cfg.run.input_csv)
    prov = provenance if provenance is not None else load_provenance(
        cfg.run.input_summary_json
    )

    result = AllocationResult(
        run_id=uuid.uuid4().hex[:12],
        generated_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        source_path=cfg.run.input_csv,
        source_run_id=prov.get("run_id"),
        source_run_status=prov.get("run_status"),
        source_ranking_validity=prov.get("ranking_validity"),
        candidates_considered=len(ranking),
    )

    # --- trust gate --------------------------------------------------------
    status = prov.get("run_status")
    if status is None:
        result.status = "DEGRADED"
        result.status_reasons.append(
            "the selection run's verdict could not be read "
            f"({cfg.run.input_summary_json}); the ranking's trustworthiness is unverified"
        )
    elif status not in TRUSTED_RUN_STATUSES:
        result.status_reasons.append(
            f"selection run_status is {status}; that ranking is not trustworthy"
        )
        if cfg.run.require_trusted_run:
            result.status = "INVALID"
            logger.error(
                "Refusing to allocate on a %s selection run. Re-run selection "
                "or set run.require_trusted_run: false to override.", status
            )
            return result
        result.status = "DEGRADED"
    elif status == "PARTIAL":
        result.status = "PARTIAL"
        result.status_reasons.append(
            "the selection ranking was PARTIAL; coverage gaps carry into these weights"
        )

    if ranking.empty:
        result.status = "INVALID"
        result.status_reasons.append("the selection ranking is empty")
        return result

    wanted = strategies or list(cfg.strategies)
    unknown = [s for s in wanted if s not in cfg.strategies]
    if unknown:
        raise ValueError(
            f"Unknown strategy/strategies {unknown}; configured: {list(cfg.strategies)}"
        )

    # Eligibility is reported against the most permissive strategy so the
    # headline count is not understated by a conservative risk filter.
    eligible_counts = [
        len(eligible_candidates(ranking, cfg.strategies[s])) for s in wanted
    ]
    result.candidates_eligible = max(eligible_counts, default=0)

    for name in wanted:
        scfg = cfg.strategies[name]
        plan = build_plan(ranking, name, scfg, cfg, price_lookup=price_lookup)
        result.plans[name] = finalize_plan(plan, cfg)
        logger.info(
            "Strategy %-13s equity %.1f%% | invested %.2f | cash %.2f | %d positions",
            name, 100 * result.plans[name].equity_weight,
            result.plans[name].invested_value, result.plans[name].cash_value,
            sum(1 for p in plan.positions if p.shares > 0),
        )

    if any(p.warnings for p in result.plans.values()) and result.status == "VALID":
        result.status = "PARTIAL"
        result.status_reasons.append(
            "one or more strategies carry position-level warnings (see the report)"
        )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-portfolio",
        description=(
            "Build three risk-profiled portfolios (conservative / balanced / "
            "aggressive) from the asset-selection ranking, reporting weights, "
            "money amounts and share counts. Research output only: no orders "
            "are placed and nothing here is financial advice."
        ),
    )
    p.add_argument("--config", default=None,
                   help="Path to the YAML config (default configs/us_equities/portfolio.yaml).")
    p.add_argument("--amount", type=float, default=None, metavar="MONEY",
                   help="Total amount to allocate (overrides capital.investable_amount).")
    p.add_argument("--input", default=None, metavar="CSV",
                   help="Selection ranking CSV (default from config).")
    p.add_argument("--strategy", nargs="+", default=None,
                   choices=["conservative", "balanced", "aggressive"],
                   help="Only build these strategies (default: all three).")
    p.add_argument("--commission", type=float, default=None, metavar="PCT",
                   help="Per-order commission as a fraction, e.g. 0.002 for 0.20%%.")
    p.add_argument("--fractional-shares", action="store_true",
                   help="Allow fractional shares instead of whole shares.")
    p.add_argument("--allow-speculative", action="store_true",
                   help="Opt every configured strategy into its speculative sleeve.")
    p.add_argument("--no-speculative", action="store_true",
                   help="Disable the speculative sleeve everywhere (budget redirected).")
    p.add_argument("--allow-untrusted-ranking", action="store_true",
                   help="Build plans even when the selection run was not trustworthy.")
    p.add_argument("--offline", action="store_true",
                   help="Skip network price lookups; only --instrument-price values are used.")
    p.add_argument("--instrument-price", nargs="+", default=None, metavar="TICKER=PRICE",
                   help="Supply instrument prices yourself, e.g. --instrument-price "
                        "SPLG=68.10 BND=73.40. Wins over the price provider.")
    p.add_argument("--output-dir", default=None, help="Override the report directory.")
    p.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR.")
    return p


def _apply_overrides(cfg: PortfolioConfig, args: argparse.Namespace) -> List[str]:
    if args.amount is not None:
        cfg.capital.investable_amount = args.amount
    if args.input:
        cfg.run.input_csv = args.input
    if args.commission is not None:
        cfg.capital.commission_per_trade_pct = args.commission
    if args.fractional_shares:
        cfg.capital.allow_fractional_shares = True
    if args.output_dir:
        cfg.run.output_dir = args.output_dir
    if args.allow_untrusted_ranking:
        cfg.run.require_trusted_run = False
    for s in cfg.strategies.values():
        if args.allow_speculative:
            s.allow_speculative = True
        if args.no_speculative:
            s.allow_speculative = False
            if s.sleeves.get("speculative", 0.0) > 0:
                # Redirect the budget so the sleeve sum stays 1.0.
                freed = s.sleeves["speculative"]
                s.sleeves["speculative"] = 0.0
                s.sleeves[s.overflow_sleeve] = (
                    s.sleeves.get(s.overflow_sleeve, 0.0) + freed
                )
    return validate_config(cfg)


def _parse_prices(items: Optional[List[str]]) -> Dict[str, float]:
    """Parse ``TICKER=PRICE`` pairs; raises ValueError with an actionable message."""
    out: Dict[str, float] = {}
    for item in items or []:
        ticker, _, raw = str(item).partition("=")
        if not ticker or not raw:
            raise ValueError(
                f"--instrument-price expects TICKER=PRICE, got {item!r}"
            )
        try:
            price = float(raw)
        except ValueError:
            raise ValueError(f"--instrument-price {item!r}: {raw!r} is not a number") from None
        if price <= 0:
            raise ValueError(f"--instrument-price {item!r}: price must be positive")
        out[ticker.strip().upper()] = price
    return out


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (PortfolioConfigError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    problems = _apply_overrides(cfg, args)
    if problems:
        print("Invalid arguments/configuration:", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        return 2
    configure_logging(args.log_level or "INFO")

    try:
        overrides = _parse_prices(args.instrument_price)
        result = run_allocation(
            cfg,
            price_lookup=build_price_lookup(
                cfg, offline=args.offline, overrides=overrides
            ),
            strategies=args.strategy,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    if not result.plans:
        print(f"No plans produced (status {result.status}).", file=sys.stderr)
        for r in result.status_reasons:
            print(f"  - {r}", file=sys.stderr)
        return 2

    paths = write_outputs(result, cfg)
    cur = cfg.capital.currency
    print(
        f"Allocation {result.run_id}: status={result.status} · "
        f"{len(result.plans)} strategies · "
        f"{cfg.capital.investable_amount:,.2f} {cur} allocated"
    )
    for name, plan in result.plans.items():
        invested = sum(1 for p in plan.positions if p.shares > 0)
        print(
            f"  {name:13s} equity {plan.equity_weight:6.1%} | "
            f"invested {plan.invested_value:>12,.2f} | cash {plan.cash_value:>10,.2f} | "
            f"{invested} holdings"
        )
    for reason in result.status_reasons:
        print(f"  - {reason}")
    print(f"Report: {paths['report_md']}")
    # Plans were produced, so the run is usable; a DEGRADED/PARTIAL caveat is
    # carried in stdout, the JSON status and the report banner. Only an
    # unusable run exits non-zero (handled above, exit 2).
    return 0


if __name__ == "__main__":
    sys.exit(main())
