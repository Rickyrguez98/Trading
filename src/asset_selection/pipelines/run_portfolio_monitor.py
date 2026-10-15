"""Portfolio monitor pipeline (US Equities Milestone 3).

Takes what you actually own -- a holdings CSV or a brokerage statement PDF --
and recommends, per position, whether to SELL, TRIM, HOLD or ADD, with the
evidence behind each call and a forecast whose accuracy has been measured.

Stages:
    1. Holdings: CSV template, or a statement PDF parsed into lots (the parse
       is written back out as an auditable CSV).
    2. Market data: daily history per holding, USDMXN, earnings calendar.
    3. Valuation in the position currency and the reporting currency,
       including the stock-vs-FX split for MXN-carried US stocks.
    4. Signals: trend and momentum, earnings events, and the selection
       pipeline's current view (latest ranking, or a fresh run on just these
       tickers with --refresh-signals).
    5. Forecast per holding plus walk-forward validation against a random walk.
    6. Decision rules and sizing (whole shares, commissions, funded ADDs).
    7. Outputs: Markdown report, JSON summary, positions CSV, order ticket CSV.

Research output only: no orders are placed and nothing here is advice.
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from trading_common.cache import Cache
from trading_common.rate_limiter import RateLimiter

from ..logging_config import configure_logging
from ..monitoring.config import MonitoringConfig, MonitoringConfigError, load_config
from ..monitoring.decision import decide_all
from ..monitoring.forecast import apply_run_level_fdr, forecast_series, make_forecaster
from ..monitoring.broker import (
    BrokerConfigError,
    infer_option,
    init_holdings,
    load_holdings,
    readiness,
    resolve_option,
)
from ..monitoring.holdings import HoldingsError, parse_number
from ..monitoring.market_data import (
    FX_SYMBOL,
    MarketData,
    OverlayMarketData,
    YFinanceMarketData,
    history_start,
)
from ..monitoring.models import REVIEW, Holding, MonitorResult, PositionReport, SelectionSignals
from ..monitoring.reporting import write_outputs
from ..monitoring.signals import (
    compute_events,
    compute_trend,
    load_selection_signals,
    merge_refreshed_sentiment,
    read_run_id,
    selection_timestamp,
)
from ..monitoring.valuation import assign_weights, compute_valuation, conversion

logger = logging.getLogger(__name__)

try:  # a local .env may hold FINNHUB_API_KEY / IBKR_FLEX_TOKEN; it is git-ignored
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - python-dotenv is a core dependency
    pass

_US_UNIVERSE_CANDIDATES = (
    "data/us_equities/processed/universe_clean.csv",
    "data/processed/universe_clean.csv",
)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trading-monitor",
        description=(
            "Recommend SELL / TRIM / HOLD / ADD for the stocks you already own. Holdings come from "
            "the broker option you pick: A = GBM statement PDF, B = holdings CSV, C = Interactive "
            "Brokers Flex (read-only). Research output only; no orders are placed."
        ),
    )
    p.add_argument("--broker", default=None, metavar="A|B|C",
                   help="Where holdings come from: A (statement PDF), B (holdings CSV), C (IBKR Flex). "
                        "Default: broker.option in the config, or inferred from --holdings.")
    p.add_argument("--holdings", default=None,
                   help="Holdings CSV or statement PDF for this run (overrides the configured path).")
    p.add_argument("--list-brokers", action="store_true",
                   help="Show broker options A/B/C, which one is selected and whether each is ready; then exit.")
    p.add_argument("--init-holdings", action="store_true",
                   help="Create your holdings CSV (broker.holdings_csv) from the template; then exit.")
    p.add_argument("--config", default=None, help="Monitoring YAML (default configs/us_equities/monitoring.yaml).")
    p.add_argument("--target", default=None,
                   help="Target portfolio CSV from trading-portfolio (e.g. data/us_equities/processed/"
                        "portfolio_balanced.csv) to rebalance against.")
    p.add_argument("--cash", type=float, default=0.0,
                   help="New money available for ADDs, in the reporting currency.")
    p.add_argument("--reporting-currency", choices=("MXN", "USD"), default=None)
    p.add_argument("--forecast", choices=("bootstrap", "chronos"), default=None,
                   help="Forecast backend. 'chronos' needs: pip install '.[forecast]' (Python >= 3.10).")
    p.add_argument("--no-validation", action="store_true",
                   help="Skip walk-forward validation (faster; forecasts then never influence decisions).")
    p.add_argument("--refresh-signals", action="store_true",
                   help="Refresh news sentiment for your holdings by running the selection pipeline on "
                        "just them (slower; does not touch your main ranking). The thesis still comes "
                        "from the full ranking: fundamentals scores are relative to the scored universe, "
                        "so re-run trading-us-equities to refresh those.")
    p.add_argument("--selection-results", default=None,
                   help="Selection results CSV to read the thesis from (default: the latest full ranking).")
    p.add_argument("--offline", action="store_true",
                   help="No network: cached data and --price/--fx values only.")
    p.add_argument("--price", nargs="+", action="extend", default=None, metavar="TICKER=PRICE",
                   help="Pin today's price for a ticker (listing currency). Repeatable.")
    p.add_argument("--fx", default=None, metavar="USDMXN=RATE", help="Pin today's USDMXN rate.")
    p.add_argument("--output-dir", default=None, help="Override the report directory.")
    p.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR.")
    return p


def _parse_pairs(items: Optional[List[str]], flag: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"{flag} expects KEY=VALUE, got {item!r}")
        key, raw = item.split("=", 1)
        val = parse_number(raw)
        if val is None or val <= 0:
            raise ValueError(f"{flag} {item!r}: value must be a positive number")
        out[key.strip().upper()] = val
    return out


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _us_universe() -> Optional[Set[str]]:
    for path in _US_UNIVERSE_CANDIDATES:
        p = Path(path)
        if p.exists():
            try:
                df = pd.read_csv(p, usecols=["ticker"])
                return {str(t).strip().upper() for t in df["ticker"].dropna()}
            except Exception:  # noqa: BLE001 - a hint only
                return None
    return None


def refresh_selection(tickers: List[str], cfg: MonitoringConfig) -> Tuple[Dict[str, SelectionSignals], Optional[str]]:
    """Run the selection pipeline on just these tickers, isolated from the main ranking."""
    from . import run_asset_selection

    work = Path(tempfile.mkdtemp(prefix="selection_refresh_", dir=_ensure(cfg.run.processed_dir)))
    argv = ["--tickers", *tickers, "--output-dir", str(work / "reports"),
            "--processed-dir", str(work / "processed"), "--log-level", "WARNING"]
    logger.info("refreshing selection signals for %d ticker(s) in %s", len(tickers), work)
    code = run_asset_selection.main(argv)
    if code not in (0, None):
        logger.warning("selection refresh exited with %s; using whatever it produced", code)
    run_id = read_run_id(work / "reports" / "asset_selection_summary.json")
    signals = load_selection_signals(work / "processed" / "asset_selection_results.csv",
                                     source="refreshed", run_id=run_id)
    return signals, str(work)


def load_targets(path: Optional[str], holdings: List[Holding]) -> Tuple[Dict[str, float], List[str]]:
    """Target weights for held tickers, renormalised over what is actually held.

    An allocation plan's weights are fractions of the whole plan, including its
    cash sleeve and any name not bought. Current weights are fractions of the
    holdings, so the targets are rescaled over the held tickers to compare
    like with like. Planned names that are not held are reported, not traded.
    Each held ticker's sleeve is copied onto the holding.
    """
    if not path:
        return {}, []
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        raise ValueError(f"{path}: target CSV needs a 'ticker' column")
    col = next((c for c in ("realized_weight_pct", "target_weight_pct", "weight_pct") if c in df.columns), None)
    if col is None:
        raise ValueError(f"{path}: target CSV needs realized_weight_pct or target_weight_pct")
    raw = {str(t).strip().upper(): float(w) / 100.0
           for t, w in zip(df["ticker"], df[col]) if pd.notna(w) and float(w) > 0}
    sleeves = ({str(t).strip().upper(): str(sl) for t, sl in zip(df["ticker"], df["sleeve"]) if pd.notna(sl)}
               if "sleeve" in df.columns else {})
    for h in holdings:
        h.sleeve = h.sleeve or sleeves.get(h.ticker)
    held = {h.ticker for h in holdings}
    mass = sum(w for t, w in raw.items() if t in held)
    targets = {t: w / mass for t, w in raw.items() if t in held} if mass > 0 else {}
    missing = sorted(t for t in raw if t not in held and t != "CASH")
    return targets, missing


def tag_allocation_instruments(holdings: List[Holding]) -> None:
    """Mark the allocation's index-core and bond-proxy tickers as strategic holdings."""
    try:
        from ..allocation.config import load_config as load_portfolio_config

        inst = load_portfolio_config(None, validate=False).instruments
    except Exception:  # noqa: BLE001 - without the portfolio config, nothing to tag
        return
    roles = {str(inst.index_core_ticker).upper(): "index_core",
             str(inst.bond_proxy_ticker).upper(): "bond_proxy"}
    for h in holdings:
        if h.sleeve is None and h.ticker in roles:
            h.sleeve = roles[h.ticker]


def _targets_for_forecast(h: Holding, last_listing: float, usdmxn: Optional[float],
                          cfg: MonitoringConfig) -> List[Tuple[str, float]]:
    targets: List[Tuple[str, float]] = []
    avg = h.avg_cost
    conv = conversion(h.currency, h.listing_currency, usdmxn)
    if avg is not None and conv is not None:
        breakeven = avg * conv                   # in listing currency, at today's FX
        if breakeven > last_listing:
            targets.append(("break-even", breakeven))
    for g in cfg.forecast.gain_targets_pct:
        targets.append((f"+{g:g}%", last_listing * (1.0 + g / 100.0)))
    return targets


def run_monitor(
    cfg: MonitoringConfig,
    holdings: List[Holding],
    market: MarketData,
    *,
    selection: Dict[str, SelectionSignals],
    selection_meta: Optional[Dict] = None,
    targets: Optional[Dict[str, float]] = None,
    missing_targets: Optional[List[str]] = None,
    new_cash: float = 0.0,
    today: Optional[date] = None,
    holdings_source: Optional[str] = None,
    extraction: Optional[Dict] = None,
    target_source: Optional[str] = None,
) -> MonitorResult:
    today = today or date.today()
    rc = cfg.run.reporting_currency.upper()
    result = MonitorResult(
        run_id=uuid.uuid4().hex[:12],
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        reporting_currency=rc, holdings_source=holdings_source, extraction=extraction,
        selection_source=selection_meta, target_source=target_source,
        forecast_backend=cfg.forecast.backend, new_cash_reporting=float(new_cash),
    )
    start = history_start(cfg.run.history_years, today=today)
    earliest = min([start] + [h.first_purchase_date for h in holdings if h.first_purchase_date])

    needs_fx = any(h.currency != rc or h.currency != h.listing_currency for h in holdings)
    fx = market.fx_usdmxn(earliest) if needs_fx else None
    if fx is not None and fx.ok:
        result.fx_rate, result.fx_as_of = fx.last_close, fx.last_date.isoformat()
    elif needs_fx:
        result.status_reasons.append(f"USDMXN unavailable ({fx.error if fx else 'not fetched'}); "
                                     "MXN/USD conversions could not be made")

    forecaster = make_forecaster(cfg.forecast)
    result.forecast_model = forecaster.model_id
    for h in holdings:
        r = PositionReport(holding=h)
        prices = market.history(h.analysis_symbol, earliest)
        if not prices.ok:
            r.warnings.append(f"NO_PRICE_DATA: {h.analysis_symbol}: {prices.error} ({prices.error_type})")
        r.valuation = compute_valuation(h, prices, fx, rc, today=today)
        if prices.ok and r.valuation.market_value is None:
            r.warnings.append("FX_UNAVAILABLE: position currency differs from the listing and USDMXN is missing")
        if r.valuation.price_age_days is not None and r.valuation.price_age_days > cfg.run.max_price_age_days:
            r.warnings.append(f"STALE_PRICE: last close {r.valuation.last_price_date}")
        r.trend = compute_trend(prices, cfg.trend)
        if h.market in ("US", "SIC"):
            r.events = compute_events(market.earnings(h.analysis_symbol), cfg.events, today=today)
        sel = selection.get(h.ticker)
        if sel is not None:
            r.selection = sel
        elif selection_meta:
            r.selection = SelectionSignals(source=selection_meta.get("source"),
                                           run_id=selection_meta.get("run_id"), in_ranking=False)
        h.name = h.name or r.selection.company_name
        h.sector = h.sector or r.selection.sector
        h.is_fund = h.is_fund or bool(r.selection.is_etf)
        if prices.ok:
            series = prices.frame["adj_close"].to_numpy()
            r.forecast = forecast_series(
                series, cfg.forecast, forecaster, key=h.analysis_symbol,
                as_of=prices.last_date.isoformat(),
                targets=_targets_for_forecast(h, float(series[-1]), result.fx_rate, cfg),
            )
        result.positions.append(r)

    if cfg.forecast.validate:
        apply_run_level_fdr(
            [(r.holding.key, r.forecast) for r in result.positions
             if r.forecast is not None and r.forecast.skill],
            cfg.forecast.validation_alpha, cfg.forecast.min_effective_windows,
        )
    total = assign_weights(result.positions)
    result.total_value_reporting = total
    result.total_cost_reporting = sum(r.valuation.cost_basis_reporting or 0.0 for r in result.positions)
    result.total_pl_reporting = sum(r.valuation.unrealized_pl_reporting or 0.0 for r in result.positions)
    result.cash_after_trades_reporting = decide_all(
        result.positions, cfg, total_value=total, targets=targets, new_cash=new_cash, today=today,
    )
    for t in missing_targets or []:
        result.status_reasons.append(f"{t} is in the target portfolio but not held (not traded by the monitor)")
    stamp = (selection_meta or {}).get("generated_at")
    if stamp:
        age = (today - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()).days
        selection_meta["age_days"] = age
        if age > cfg.run.max_selection_age_days:
            result.status_reasons.append(
                f"the selection ranking used for the thesis check is {age} days old "
                f"({str(stamp)[:10]}); re-run trading-us-equities on the full universe to refresh it"
            )
    _set_status(result, extraction)
    return result


def _set_status(result: MonitorResult, extraction: Optional[Dict]) -> None:
    valued = [r for r in result.positions if r.valuation.market_value_reporting is not None]
    if not valued:
        result.status = "INVALID"
        result.status_reasons.insert(0, "no position could be valued")
        return
    reasons = []
    review = [r.holding.ticker for r in result.positions if r.recommendation.action == REVIEW]
    if review:
        reasons.append(f"{len(review)} position(s) need review (no usable price): {', '.join(review)}")
    if extraction and extraction.get("confidence") == "low":
        reasons.append("statement extraction confidence is LOW -- audit the review CSV")
    elif extraction and extraction.get("confidence") == "medium":
        reasons.append("statement extraction confidence is MEDIUM -- audit the review CSV")
    if not result.selection_source:
        reasons.append("no selection results: the investment thesis could not be checked")
    stale = [r.holding.ticker for r in result.positions
             if any(w.startswith("STALE_PRICE") for w in r.warnings)]
    if stale:
        reasons.append(f"stale prices for {', '.join(stale)}")
    degraded = (not result.selection_source) or (extraction or {}).get("confidence") == "low"
    result.status_reasons = reasons + result.status_reasons
    result.status = "DEGRADED" if degraded else ("PARTIAL" if result.status_reasons else "VALID")


def _print_brokers(cfg: MonitoringConfig, selected) -> int:
    print("Broker options for trading-monitor (pick one: --broker A|B|C or broker.option in the config)\n")
    for r in readiness(cfg.broker):
        mark = "->" if r.option.code == selected.code else "  "
        state = "ready" if r.ready else "not ready"
        print(f"{mark} {r.option.code}  {r.option.label:<52} [{state}] {r.detail}")
        print(f"      how: {r.option.how}")
    print("\nDetails and trade-offs: docs/us_equities/broker-integration-options.md")
    return 0


def _ensure(path: str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    configure_logging(args.log_level or "INFO")
    try:
        cfg = load_config(args.config)
    except (MonitoringConfigError, FileNotFoundError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.reporting_currency:
        cfg.run.reporting_currency = args.reporting_currency
    if args.forecast:
        cfg.forecast.backend = args.forecast
    if args.no_validation:
        cfg.forecast.validate = False
    if args.output_dir:
        cfg.run.output_dir = args.output_dir

    try:
        option = (resolve_option(args.broker) if args.broker
                  else infer_option(args.holdings) or resolve_option(cfg.broker.option))
    except BrokerConfigError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    if args.list_brokers:
        return _print_brokers(cfg, option)
    if args.init_holdings:
        try:
            created = init_holdings(cfg.broker)
        except BrokerConfigError as exc:
            print(f"Input error: {exc}", file=sys.stderr)
            return 2
        print(f"Created {created} from the template. Replace the example rows with your holdings "
              "(one row per purchase), then run: trading-monitor")
        return 0

    try:
        price_pins = _parse_pairs(args.price, "--price")
        fx_pin = _parse_pairs([args.fx] if args.fx else None, "--fx")
        if fx_pin and set(fx_pin) != {"USDMXN"}:
            raise ValueError("--fx expects USDMXN=RATE")
        processed = _ensure(cfg.run.processed_dir)
        if option.code == "C" and args.offline:
            raise ValueError("option C downloads your positions from IBKR and cannot run with --offline")
        loaded = load_holdings(option, cfg.broker, path=args.holdings, processed_dir=processed,
                               us_universe=_us_universe())
    except (HoldingsError, ValueError, FileNotFoundError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    holdings, extraction, source, problems = loaded.holdings, loaded.extraction, loaded.source, loaded.problems
    logger.info("holdings from broker option %s (%s): %d position(s)", option.code, option.label, len(holdings))

    rl = RateLimiter(0.4)
    cache = Cache(directory=cfg.run.cache_dir, enabled=True, default_ttl=cfg.run.cache_ttl_seconds)
    market: MarketData = YFinanceMarketData(cache, rl, ttl_seconds=cfg.run.cache_ttl_seconds,
                                            offline=args.offline)
    pins = dict(price_pins)
    if fx_pin:
        pins[FX_SYMBOL] = fx_pin["USDMXN"]
    if pins:
        symbol_pins = {}
        by_ticker = {h.ticker: h.analysis_symbol for h in holdings}
        for k, v in pins.items():
            symbol_pins[by_ticker.get(k, k)] = v
        market = OverlayMarketData(market, symbol_pins)

    path = args.selection_results or cfg.run.selection_results_csv
    # The default summary describes the default ranking only; a ranking passed
    # with --selection-results is dated by its own file, never by another run.
    summary = cfg.run.selection_summary_json if args.selection_results is None else None
    run_id = read_run_id(summary)
    selection = load_selection_signals(path, source="full_ranking", run_id=run_id)
    stamp = selection_timestamp(path, summary)
    meta = {"source": "full_ranking", "path": path, "run_id": run_id,
            "generated_at": stamp.isoformat() if stamp else None} if selection else None

    if args.refresh_signals and not args.offline:
        # Fundamentals scores are z-scores across the scored set, so a small
        # rescore cannot replace the full ranking's thesis; only the absolute,
        # per-stock news sentiment is refreshed.
        tickers = sorted({h.ticker for h in holdings if h.in_selection_coverage})
        if tickers:
            fresh, where = refresh_selection(tickers, cfg)
            configure_logging(args.log_level or "INFO")     # the selection run resets logging
            selection = merge_refreshed_sentiment(selection, fresh)
            meta = dict(meta or {"source": "full_ranking"})
            meta["sentiment_refreshed_at"] = datetime.now(timezone.utc).isoformat()
            meta["refresh_path"] = where

    try:
        targets, missing = load_targets(args.target, holdings)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    tag_allocation_instruments(holdings)

    result = run_monitor(
        cfg, holdings, market, selection=selection, selection_meta=meta,
        targets=targets, missing_targets=missing, new_cash=args.cash,
        holdings_source=source, extraction=extraction, target_source=args.target,
    )
    if problems:
        result.status_reasons.extend(problems)
        if result.status == "VALID":
            result.status = "PARTIAL"
    paths = write_outputs(result, Path(cfg.run.output_dir), processed)

    rc = result.reporting_currency
    print(f"Portfolio monitor {result.run_id}: status={result.status} · {len(result.positions)} positions · "
          f"value {result.total_value_reporting:,.2f} {rc} · P/L {result.total_pl_reporting:+,.2f} {rc}")
    for r in sorted(result.positions, key=lambda x: x.recommendation.action):
        rec = r.recommendation
        delta = f"{rec.shares_delta:+g} sh" if rec.shares_delta else ""
        print(f"  {r.holding.ticker:<9} {rec.action:<6} {delta:<10} {rec.thesis_status:<9} {rec.rule[:90]}")
    for reason in result.status_reasons:
        print(f"  - {reason}")
    print(f"Report: {paths['markdown']}")
    print(f"Orders: {paths['orders_csv']}")
    return 2 if result.status == "INVALID" else 0


if __name__ == "__main__":
    sys.exit(main())
