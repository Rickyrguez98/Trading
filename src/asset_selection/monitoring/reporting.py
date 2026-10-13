"""Write a monitoring run as Markdown, CSV and JSON.

The Markdown report leads with the actions, then shows every position in both
percentage and money, then explains each recommendation with the evidence that
produced it and the forecast's *measured* accuracy -- so a confident-looking
number never appears without the test that backs (or does not back) it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .models import ADD, HOLD, REVIEW, SELL, TRIM, MonitorResult, PositionReport

_ACTION_ORDER = {SELL: 0, TRIM: 1, ADD: 2, REVIEW: 3, HOLD: 4}
DISCLAIMER = (
    "Research output only — **not financial advice**. No orders are placed. "
    "Forecasts are probabilistic and are shown with their measured accuracy against a random walk."
)


def write_outputs(result: MonitorResult, output_dir: Path, processed_dir: Path) -> Dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": output_dir / "portfolio_monitor.md",
        "json": output_dir / "portfolio_monitor_summary.json",
        "positions_csv": processed_dir / "monitor_positions.csv",
        "orders_csv": processed_dir / "monitor_orders.csv",
    }
    paths["markdown"].write_text(render_markdown(result), encoding="utf-8")
    paths["json"].write_text(
        json.dumps(result.to_dict(), indent=2, allow_nan=False, default=str), encoding="utf-8"
    )
    position_rows(result).to_csv(paths["positions_csv"], index=False)
    order_rows(result).to_csv(paths["orders_csv"], index=False)
    return paths


# ---------------------------------------------------------------------------
# Tabular outputs
# ---------------------------------------------------------------------------

def _sorted(result: MonitorResult) -> List[PositionReport]:
    return sorted(result.positions, key=lambda r: (_ACTION_ORDER.get(r.recommendation.action, 9),
                                                    -(r.valuation.weight or 0.0)))


def position_rows(result: MonitorResult) -> pd.DataFrame:
    rows = []
    for r in _sorted(result):
        h, v, rec, t = r.holding, r.valuation, r.recommendation, r.trend
        fc = r.forecast
        mid = None
        if fc is not None and fc.horizons:
            mid = fc.horizons[len(fc.horizons) // 2]
        rows.append({
            "ticker": h.ticker, "market": h.market, "currency": h.currency, "name": h.name,
            "sector": h.sector, "shares": h.shares, "avg_cost": h.avg_cost,
            "cost_basis": h.cost_basis, "first_purchase_date": h.first_purchase_date,
            "last_price": v.last_price, "listing_currency": h.listing_currency,
            "price_date": v.last_price_date, "market_value": v.market_value,
            "unrealized_pl": v.unrealized_pl, "unrealized_pl_pct": v.unrealized_pl_pct,
            f"market_value_{result.reporting_currency.lower()}": v.market_value_reporting,
            f"unrealized_pl_{result.reporting_currency.lower()}": v.unrealized_pl_reporting,
            "weight_pct": None if v.weight is None else 100 * v.weight,
            "holding_days": v.holding_days, "stock_return_pct": v.stock_return_pct,
            "fx_return_pct": v.fx_return_pct, "trend_state": t.trend_state,
            "momentum_12_1_pct": t.momentum_12_1_pct, "volatility_pct": t.volatility_ann_pct,
            "next_earnings_date": r.events.next_earnings_date,
            "fundamentals_score": r.selection.fundamentals_score,
            "sentiment_score": r.selection.effective_sentiment_score,
            "candidate_role": r.selection.candidate_role,
            "thesis_status": rec.thesis_status, "action": rec.action,
            "shares_delta": rec.shares_delta, "value_delta": rec.value_delta_reporting,
            "target_weight_pct": None if rec.target_weight is None else 100 * rec.target_weight,
            "composite_score": rec.composite_score, "confidence": rec.confidence,
            "forecast_backend": None if fc is None else fc.backend,
            "forecast_horizon_days": None if mid is None else mid.horizon_days,
            "forecast_p_up": None if mid is None else mid.p_up,
            "forecast_informative": None if (fc is None or mid is None) else fc.informative_at(mid.horizon_days),
            "rule": rec.rule, "outlook": rec.outlook,
            "blockers": "; ".join(rec.blockers),
            "warnings": "; ".join(sorted(set(r.warnings + h.warnings))),
        })
    return pd.DataFrame(rows)


def order_rows(result: MonitorResult) -> pd.DataFrame:
    """Only executable changes: the ticket a person would key into the broker."""
    rows = []
    for r in _sorted(result):
        rec = r.recommendation
        if rec.action not in (SELL, TRIM, ADD) or rec.shares_delta == 0:
            continue
        rows.append({
            "ticker": r.holding.ticker,
            "market": r.holding.market,
            "side": "BUY" if rec.shares_delta > 0 else "SELL",
            "shares": abs(rec.shares_delta),
            "approx_price": r.valuation.last_price,
            "price_currency": r.holding.listing_currency,
            f"approx_value_{result.reporting_currency.lower()}": abs(rec.value_delta_reporting),
            f"commission_{result.reporting_currency.lower()}": rec.commission_reporting,
            "action": rec.action,
            "reason": rec.rule,
        })
    return pd.DataFrame(rows, columns=[
        "ticker", "market", "side", "shares", "approx_price", "price_currency",
        f"approx_value_{result.reporting_currency.lower()}",
        f"commission_{result.reporting_currency.lower()}", "action", "reason",
    ])


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _m(x: Optional[float], ccy: str = "", nd: int = 2) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}{ccy + ' ' if ccy else ''}{abs(x):,.{nd}f}"


def _p(x: Optional[float], nd: int = 1, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.{nd}f}%" if signed else f"{x:.{nd}f}%"


def _w(x: Optional[float]) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def render_markdown(result: MonitorResult) -> str:
    ccy = result.reporting_currency
    L: List[str] = []
    L.append("# Portfolio monitor — hold, trim, sell or add")
    L.append("")
    L.append(f"> **Status: {result.status}** — generated {result.generated_at} · run `{result.run_id}`")
    for reason in result.status_reasons:
        L.append(f"> - {reason}")
    L.append(f"> {DISCLAIMER}")
    L.append("")
    fx = f"USDMXN {result.fx_rate:.4f} ({result.fx_as_of})" if result.fx_rate else "no FX rate needed/available"
    pl_pct = (100 * result.total_pl_reporting / result.total_cost_reporting) if result.total_cost_reporting else None
    L.append(
        f"Portfolio value **{_m(result.total_value_reporting, ccy)}** · cost {_m(result.total_cost_reporting, ccy)} · "
        f"unrealized P/L **{_m(result.total_pl_reporting, ccy)} ({_p(pl_pct)})** · {fx}"
    )
    if result.new_cash_reporting:
        L.append(f"New cash available: {_m(result.new_cash_reporting, ccy)} · cash after proposed trades: "
                 f"{_m(result.cash_after_trades_reporting, ccy)}")
    counts: Dict[str, int] = {}
    for r in result.positions:
        counts[r.recommendation.action] = counts.get(r.recommendation.action, 0) + 1
    L.append("Actions: " + " · ".join(f"**{a}** {counts[a]}" for a in sorted(counts, key=_ACTION_ORDER.get)))
    L.append("")

    sel = result.selection_source or {}
    if sel:
        when = (sel.get("generated_at") or "")[:10] or "unknown date"
        age = f", {sel['age_days']} days old" if sel.get("age_days") is not None else ""
        L.append(f"Thesis checked against the full selection ranking from {when}{age}. Fundamentals scores "
                 "are relative to the scored universe, so refresh them by re-running `trading-us-equities` "
                 "on the full universe.")
        if sel.get("sentiment_refreshed_at"):
            L.append(f"News sentiment refreshed for the held tickers at {sel['sentiment_refreshed_at'][:16]}Z.")
    else:
        L.append("No selection results were available, so no investment thesis could be checked.")
    L.append("")

    if result.extraction:
        _extraction_section(L, result.extraction)

    L.append("## Recommended actions")
    L.append("")
    L.append(f"| Ticker | Action | Shares | Money ({ccy}) | Weight now → after | Thesis | Confidence | Why |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in _sorted(result):
        rec = r.recommendation
        sh = "—" if rec.shares_delta == 0 else f"{rec.shares_delta:+g}"
        money = "—" if rec.value_delta_reporting == 0 else _m(rec.value_delta_reporting)
        after = _w(rec.post_trade_weight) if rec.post_trade_weight is not None else _w(rec.current_weight)
        target = f" (target {_w(rec.target_weight)})" if rec.target_weight is not None else ""
        L.append(f"| {r.holding.ticker} | **{rec.action}** | {sh} | {money} | "
                 f"{_w(rec.current_weight)} → {after}{target} | {rec.thesis_status} | {rec.confidence} | "
                 f"{_md(rec.rule)} |")
    L.append("")

    L.append("## Positions — percentage and money")
    L.append("")
    L.append(f"| Ticker | Market | Shares | Avg cost | Price | Value ({ccy}) | P/L ({ccy}) | P/L % | "
             f"Weight | Held | Stock / FX |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in _sorted(result):
        h, v = r.holding, r.valuation
        split = (f"{_p(v.stock_return_pct)} / {_p(v.fx_return_pct)}"
                 if v.stock_return_pct is not None else "—")
        held = f"{v.holding_days}d" if v.holding_days is not None else "—"
        L.append(f"| {h.ticker} | {h.market} | {h.shares:g} | {_m(h.avg_cost, h.currency)} | "
                 f"{_m(v.last_price, h.listing_currency)} | {_m(v.market_value_reporting)} | "
                 f"{_m(v.unrealized_pl_reporting)} | {_p(v.unrealized_pl_pct)} | {_w(v.weight)} | "
                 f"{held} | {split} |")
    L.append("")

    L.append("## Position detail")
    L.append("")
    for r in _sorted(result):
        _position_section(L, r, result)

    _validation_section(L, result)
    _method_section(L, result)
    return "\n".join(L) + "\n"


def _position_section(L: List[str], r: PositionReport, result: MonitorResult) -> None:
    h, rec, t, e = r.holding, r.recommendation, r.trend, r.events
    title = f"{h.ticker}" + (f" — {h.name}" if h.name else "")
    L.append(f"### {title}: **{rec.action}**")
    L.append("")
    L.append(f"- **Why:** {rec.rule}")
    if rec.outlook:
        L.append(f"- **Outlook:** {rec.outlook}")
    L.append(f"- **Review by:** {rec.review_by}" + (
        f" (earnings {e.next_earnings_date}, in {e.days_to_earnings} days)" if e.next_earnings_date else ""))
    for b in rec.blockers:
        L.append(f"- ⚠ {b}")
    L.append("")
    L.append(f"Evidence (composite **{rec.composite_score:+.2f}**, confidence {rec.confidence}):")
    L.append("")
    for ev in rec.evidence:
        mark = "＋" if ev.score > 0 else ("－" if ev.score < 0 else "·")
        L.append(f"- {mark} `{ev.factor}` ({ev.score:+.2f}) — {ev.summary}")
    L.append("")
    ret = " · ".join(f"{k} {_p(val)}" for k, val in t.returns_pct.items() if val is not None)
    if ret:
        L.append(f"Returns: {ret} · 52w high {_p(t.pct_from_52w_high)} · "
                 f"volatility {_p(t.volatility_ann_pct, 0, signed=False)}")
        L.append("")
    fc = r.forecast
    if fc is not None and fc.status == "ok" and fc.horizons:
        L.append(f"Forecast — `{fc.backend}` ({fc.model_id}), from {fc.as_of}, price "
                 f"{_m(fc.last_price, h.listing_currency)}:")
        L.append("")
        L.append("| Horizon | 10% | Median | 90% | P(up) | Skill vs random walk | q (FDR) | "
                 "Independent windows | Hit rate | Informative |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for hf in fc.horizons:
            sk = fc.skill.get(hf.horizon_days)
            q10 = hf.quantiles.get(0.1)
            q90 = hf.quantiles.get(0.9)
            if sk is None or sk.p_value is None:
                tail = "not tested | — | — | — | no |"
            else:
                adj = "—" if sk.p_value_adjusted is None else f"{sk.p_value_adjusted:.3f}"
                hit = ("n/a" if not fc.directional
                       else ("—" if sk.hit_rate is None else f"{100 * sk.hit_rate:.0f}%"))
                verdict = "yes" if sk.informative else (
                    "too few" if sk.p_value_adjusted is None else "no")
                tail = (f"{100 * sk.skill:+.1f}% | {adj} | {sk.effective_windows:.0f} | "
                        f"{hit} | {verdict} |")
            L.append(f"| {hf.horizon_days}d | {_m(q10)} | {_m(hf.median)} | {_m(q90)} | "
                     f"{100 * hf.p_up:.0f}% | " + tail)
        L.append("")
        if fc.targets:
            longest = max(x.horizon_days for x in fc.horizons)
            L.append(f"| Target | Price | P(above) @ {', '.join(f'{x.horizon_days}d' for x in fc.horizons)} | "
                     f"P(touch) by {longest}d | Median path reaches it |")
            L.append("|---|---|---|---|---|")
            for tg in fc.targets:
                above = " / ".join(f"{100 * tg.p_above_at[x.horizon_days]:.0f}%" for x in fc.horizons)
                touch = "—" if not tg.p_touch_by else f"{100 * tg.p_touch_by[longest]:.0f}%"
                cross = "not within window" if tg.median_crossing_day is None else f"day {tg.median_crossing_day}"
                L.append(f"| {tg.label} | {_m(tg.target_price, h.listing_currency)} | {above} | {touch} | {cross} |")
            L.append("")
        for n in fc.notes:
            L.append(f"> {n}")
        L.append("")
    elif fc is not None:
        L.append(f"Forecast unavailable: {fc.status}" + (f" — {fc.error}" if fc.error else "")
                 + "".join(f"; {n}" for n in fc.notes))
        L.append("")
    warns = sorted(set(r.warnings + h.warnings))
    if warns:
        for w in warns:
            L.append(f"- _{w}_")
        L.append("")


def _validation_section(L: List[str], result: MonitorResult) -> None:
    tested = [r.forecast for r in result.positions if r.forecast and r.forecast.skill]
    if not tested:
        return
    horizons = sorted({h for f in tested for h in f.skill})
    L.append("## Does the forecast beat a random walk?")
    L.append("")
    L.append("Each forecast was replayed on past data (walk-forward) and scored against a zero-drift random "
             "walk with the same volatility. *Informative* means significantly better at the 5% level after "
             "controlling the false-discovery rate across every stock and horizon in this run "
             "(Benjamini–Hochberg), with enough independent (non-overlapping) windows behind it. Testing many "
             "stocks guarantees a few lucky 'wins'; the correction is what keeps them out of your decisions. "
             "Only a model with a directional view can let a surviving edge influence an action.")
    L.append("")
    L.append("| Horizon | Positions tested | Informative | Median skill |")
    L.append("|---|---|---|---|")
    for h in horizons:
        sk = [f.skill[h] for f in tested if h in f.skill]
        inf = sum(1 for s in sk if s.informative)
        med = sorted(s.skill for s in sk)[len(sk) // 2] if sk else None
        L.append(f"| {h}d | {len(sk)} | {inf} | {'—' if med is None else f'{100 * med:+.1f}%'} |")
    L.append("")


def _extraction_section(L: List[str], ex: Dict) -> None:
    L.append("## Statement extraction")
    L.append("")
    L.append(f"Read **{ex.get('source')}** as a `{ex.get('statement_type')}` statement: "
             f"{ex.get('lots')} lot(s) from {ex.get('holdings_rows')} holdings row(s); "
             f"{ex.get('dates_attached')} purchase date(s) attached from {ex.get('buy_rows')} buy movement(s). "
             f"Extraction confidence: **{ex.get('confidence')}**.")
    if ex.get("review_csv"):
        L.append(f"Audit and correct the extracted holdings in `{ex['review_csv']}`, then re-run with "
                 f"`--holdings {ex['review_csv']}`.")
    for w in (ex.get("warnings") or [])[:20]:
        L.append(f"- {w}")
    for s in (ex.get("skipped") or [])[:20]:
        L.append(f"- skipped: {s}")
    L.append("")


def _method_section(L: List[str], result: MonitorResult) -> None:
    L.append("## How these recommendations are made")
    L.append("")
    L.append("- **SELL** only when the investment thesis is broken (fundamentals collapsed or the name "
             "fell out of the selection model with weak fundamentals). A loss alone, or a gain alone, never "
             "triggers a sale.")
    L.append("- **TRIM** when a position outgrew its target weight band (or the per-name cap), or when the "
             "thesis weakened *and* the evidence turned negative.")
    L.append("- **ADD** only with an intact thesis, strong combined evidence, room below the target, no "
             "earnings report inside the blackout window, and actual money to fund it.")
    L.append("- **HOLD** otherwise. **REVIEW** when the position cannot be valued.")
    L.append("- Forecasts influence a decision only at horizons where walk-forward testing showed a "
             "statistically significant edge over a random walk.")
    L.append("- Taxes (ISR on gains), FX spreads and order slippage are not modelled.")
    L.append("")
    L.append(f"_{DISCLAIMER}_")


def _md(text: Optional[str]) -> str:
    return (text or "").replace("|", "\\|")
