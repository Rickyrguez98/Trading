"""Allocation outputs: CSV, Markdown and JSON.

The Markdown report is the human deliverable: for each strategy it shows the
sleeve budget, every position in **both percentage and money**, the order
ticket with share counts and commissions, the diversification profile by
business sector, and the warnings. It opens with a run-status banner and
closes with the research disclaimer.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from trading_common.io import ensure_dir, write_csv, write_json

from .config import SLEEVES, PortfolioConfig
from .models import AllocationResult, StrategyPlan
from .money import order_rows

_SLEEVE_LABEL = {
    "cash": "Cash",
    "bond_proxy": "Bonds (stability)",
    "index_core": "Index core (S&P 500)",
    "equity_core": "Quality single names",
    "growth_satellite": "Growth satellites",
    "speculative": "Speculative (capped)",
}


def _money(v: float, currency: str = "USD") -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}" if currency == "USD" else f"{sign}{abs(v):,.2f} {currency}"


# ---------------------------------------------------------------------------
# Tabular outputs
# ---------------------------------------------------------------------------

def plan_rows(plan: StrategyPlan) -> List[Dict[str, Any]]:
    rows = []
    for p in plan.positions:
        row = p.to_row()
        row["strategy"] = plan.strategy
        row["warnings"] = "; ".join(row["warnings"])
        rows.append(row)
    return rows


def write_outputs(
    result: AllocationResult, cfg: PortfolioConfig
) -> Dict[str, str]:
    """Write the CSV / Markdown / JSON set; returns the paths written."""
    out_dir = ensure_dir(cfg.run.output_dir)
    processed = ensure_dir(cfg.run.processed_dir)
    paths: Dict[str, str] = {}

    all_rows: List[Dict[str, Any]] = []
    for name, plan in result.plans.items():
        rows = plan_rows(plan)
        all_rows.extend(rows)
        p = processed / f"portfolio_{name}.csv"
        write_csv(p, pd.DataFrame(rows))
        paths[f"{name}_csv"] = str(p)

    combined = processed / "portfolio_allocations.csv"
    write_csv(combined, pd.DataFrame(all_rows))
    paths["combined_csv"] = str(combined)

    summary = out_dir / "portfolio_summary.json"
    write_json(summary, result.to_dict())
    paths["summary_json"] = str(summary)

    md = out_dir / "portfolio_allocation.md"
    md.write_text(render_markdown(result, cfg), encoding="utf-8")
    paths["report_md"] = str(md)
    return paths


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _banner(result: AllocationResult) -> str:
    icons = {"VALID": "OK", "PARTIAL": "PARTIAL", "DEGRADED": "DEGRADED",
             "INVALID": "INVALID"}
    lines = [
        f"> **Allocation status: {icons.get(result.status, result.status)}** — "
        f"generated {result.generated_at} from selection run "
        f"`{result.source_run_id or 'unknown'}` "
        f"(selection status: {result.source_run_status or 'unknown'}, "
        f"ranking: {result.source_ranking_validity or 'unknown'})."
    ]
    for reason in result.status_reasons:
        lines.append(f"> - {reason}")
    if result.status != "VALID":
        lines.append(
            "> Treat the plans below with these caveats; a non-valid allocation "
            "is not equivalent to a valid one."
        )
    lines.append(
        "> Research output only — **not financial advice**. No orders are placed."
    )
    return "\n".join(lines)


def _comparison_table(result: AllocationResult, currency: str) -> List[str]:
    lines = [
        "| Strategy | Equity | Index core | Single names | Growth | Speculative "
        "| Cash+Bonds | Largest position | Eff. positions | Invested |",
        "|---" * 10 + "|",
    ]
    for name, plan in result.plans.items():
        by_sleeve = {s.sleeve: s.allocated_weight for s in plan.sleeves}
        stability = by_sleeve.get("cash", 0.0) + by_sleeve.get("bond_proxy", 0.0)
        lines.append(
            f"| **{name}** | {plan.equity_weight:.1%} "
            f"| {by_sleeve.get('index_core', 0.0):.1%} "
            f"| {by_sleeve.get('equity_core', 0.0):.1%} "
            f"| {by_sleeve.get('growth_satellite', 0.0):.1%} "
            f"| {by_sleeve.get('speculative', 0.0):.1%} "
            f"| {stability:.1%} "
            f"| {plan.largest_position_weight:.1%} "
            f"| {plan.effective_positions:.1f} "
            f"| {_money(plan.invested_value, currency)} |"
        )
    return lines


def _plan_section(plan: StrategyPlan, cfg: PortfolioConfig) -> List[str]:
    cur = cfg.capital.currency
    lines = [f"## {plan.label}", ""]
    if plan.description:
        lines += [plan.description, ""]
    lines.append(
        f"Capital allocated: **{_money(plan.investable_amount, cur)}** — invested "
        f"{_money(plan.invested_value, cur)}, commissions "
        f"{_money(plan.commission_total, cur)}, cash "
        f"{_money(plan.cash_value, cur)}."
    )
    lines.append("")

    lines.append("### Sleeve budget")
    lines.append("")
    lines.append("| Sleeve | Target | Allocated | Money | Positions | Notes |")
    lines.append("|---" * 6 + "|")
    for sleeve in SLEEVES:
        res = next((s for s in plan.sleeves if s.sleeve == sleeve), None)
        if res is None or (res.target_budget <= 0 and res.allocated_weight <= 0):
            continue
        money = res.allocated_weight * plan.investable_amount
        lines.append(
            f"| {_SLEEVE_LABEL.get(sleeve, sleeve)} | {res.target_budget:.1%} "
            f"| {res.allocated_weight:.1%} | {_money(money, cur)} "
            f"| {res.position_count} | {'; '.join(res.notes) or '—'} |"
        )
    lines.append("")

    lines.append("### Positions — percentage and money")
    lines.append("")
    lines.append(
        "| Ticker | Sleeve | Sector | Target % | Actual % | Money | Shares "
        "| Price | Commission | Score | Vol | Capped | Warnings |"
    )
    lines.append("|---" * 13 + "|")
    for p in sorted(plan.positions, key=lambda x: (-x.target_weight, x.ticker)):
        vol = f"{p.volatility_pct:.0%}" if p.volatility_pct is not None else "—"
        score = f"{p.score:.1f}" if p.score is not None else "—"
        price = f"{p.price:,.2f}" if p.price else "—"
        shares = f"{p.shares:g}" if p.shares else "0"
        lines.append(
            f"| {p.ticker} | {_SLEEVE_LABEL.get(p.sleeve, p.sleeve)} "
            f"| {p.sector or '—'} | {p.target_weight:.2%} | {p.realized_weight:.2%} "
            f"| {_money(p.invested_value, cur)} | {shares} | {price} "
            f"| {_money(p.commission, cur)} | {score} | {vol} "
            f"| {p.capped_by or '—'} | {', '.join(sorted(set(p.warnings))) or '—'} |"
        )
    lines.append("")

    orders = order_rows(plan)
    lines.append(f"### Order ticket ({len(orders)} orders)")
    lines.append("")
    if orders:
        lines.append("| Ticker | Action | Shares | Price | Gross | Commission | Total |")
        lines.append("|---" * 7 + "|")
        for o in orders:
            lines.append(
                f"| {o['ticker']} | {o['action']} | {o['shares']:g} "
                f"| {o['price']:,.2f} | {_money(float(o['gross_value']), cur)} "
                f"| {_money(float(o['commission']), cur)} "
                f"| {_money(float(o['total_cost']), cur)} |"
            )
    else:
        lines.append("_No executable orders at this capital level._")
    lines.append("")

    lines.append("### Diversification by business sector")
    lines.append("")
    if plan.sector_weights:
        lines.append("| Sector | Weight | Money |")
        lines.append("|---|---|---|")
        for sec, w in sorted(plan.sector_weights.items(), key=lambda kv: -kv[1]):
            lines.append(
                f"| {sec} | {w:.1%} | {_money(w * plan.investable_amount, cur)} |"
            )
    else:
        lines.append("_No invested positions._")
    lines.append("")

    if plan.warnings or plan.notes:
        lines.append("### Warnings and notes")
        lines.append("")
        for w in sorted(set(plan.warnings)):
            lines.append(f"- `{w}`")
        for n in plan.notes:
            lines.append(f"- {n}")
        lines.append("")
    return lines


def render_markdown(result: AllocationResult, cfg: PortfolioConfig) -> str:
    cur = cfg.capital.currency
    lines = ["# Portfolio allocation — three risk strategies", ""]
    lines.append(_banner(result))
    lines.append("")
    lines.append(
        f"Capital: **{_money(cfg.capital.investable_amount, cur)}** · "
        f"commission {cfg.capital.commission_per_trade_pct:.2%} per order · "
        f"{'fractional' if cfg.capital.allow_fractional_shares else 'whole'} shares · "
        f"{result.candidates_eligible} of {result.candidates_considered} ranked "
        "names were eligible to hold."
    )
    lines.append("")

    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "- Each strategy is a **sleeve budget** covering the whole portfolio: "
        "cash and bonds for stability, a broad **S&P 500 index core** for market "
        "coverage, **quality single names**, small **growth satellites**, and an "
        "opt-in **speculative** sleeve that is hard-capped."
    )
    lines.append(
        "- Single-name weights come from the score the selection pipeline "
        "already computed (fundamentals, growth, quality, valuation and "
        "confidence-adjusted news sentiment, minus risk penalties), tilted "
        "toward lower volatility, then constrained by per-name and per-sector "
        "caps so no business type dominates."
    )
    lines.append(
        "- `Target %` is the intended weight; `Actual %` is what whole-share "
        "rounding achieved. Money columns are the actual amounts."
    )
    lines.append(
        "- A speculative position is research-only by construction: selection "
        "excludes those names from allocation by default and they appear here "
        "only because the strategy opted in."
    )
    lines.append("")

    lines.append("## Strategy comparison")
    lines.append("")
    lines += _comparison_table(result, cur)
    lines.append("")

    for plan in result.plans.values():
        lines += _plan_section(plan, cfg)

    lines.append("---")
    lines.append(
        "> **Disclaimer.** Research and educational output only — **not "
        "financial advice** and not a recommendation to buy or sell anything. "
        "These weights are a transparent, rule-based construction from public "
        "fundamental and news data; they are **not** an optimized or backtested "
        "portfolio, no expected return or risk forecast is implied, and past "
        "behavior does not guarantee future results. Nothing here places "
        "orders, connects to a broker, or advises how much money to risk. "
        "The speculative sleeve can lose its entire value."
    )
    lines.append("")
    return "\n".join(lines)
