"""Your portfolio from broker trade confirmations.

Upload any number of confirmation PDFs; every trade is read, duplicates from
re-uploaded files are skipped, and the result is kept as a ledger in
``data/app/portfolio/executions.csv`` (git-ignored). From the ledger come the
orders, the buy lots matched to their sales (first in, first out), live
prices for what is still open, and the CSV downloads.

Amounts are in the account's currency (USD for GBM's US-dollar account). An
open lot's result is "if sold now": market value minus what it cost,
before the sale's commission.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asset_selection.monitoring.confirmations import ConfirmationParseError, parse_confirmation_pdf
from asset_selection.monitoring.ledger import (
    BUY,
    LedgerView,
    OrdersTableError,
    consolidate,
    csv_safe,
    executions_from_table,
    holdings_csv_text,
    merge_new,
    read_executions,
    write_executions,
)

from .market import MarketData, MarketDataError, normalize_symbol
from .outlook import HORIZONS, exit_plan, month_paths, prognosis, stock_outlook
from .paths import Paths

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_FILES = 500


class PortfolioError(ValueError):
    """An upload batch that cannot be processed at all."""


@dataclass
class FileResult:
    name: str
    trades: int = 0
    added: int = 0
    duplicates: int = 0
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ImportResult:
    files: List[FileResult]
    added: int = 0
    duplicates: int = 0
    at: str = ""

    @property
    def failed(self) -> List[FileResult]:
        return [f for f in self.files if f.error]

    def message(self) -> str:
        read = len(self.files) - len(self.failed)
        trades = sum(f.trades for f in self.files)
        text = (f"Read {read} of {len(self.files)} file(s): {trades} trade(s), {self.added} new, "
                f"{self.duplicates} already imported")
        if self.failed:
            text += f"; {len(self.failed)} file(s) could not be read (details below)"
        return text


class Portfolio:
    def __init__(self, paths: Paths, market: MarketData) -> None:
        self.dir = paths.app_dir / "portfolio"
        self.ledger_path = self.dir / "executions.csv"
        self.report_path = self.dir / "last_import.json"
        self.market = market

    # ------------------------------------------------------------------ data
    def view(self) -> LedgerView:
        return consolidate(read_executions(self.ledger_path))

    def has_data(self) -> bool:
        return self.ledger_path.exists() and bool(read_executions(self.ledger_path))

    def import_pdfs(self, files: Sequence[Tuple[str, bytes]]) -> ImportResult:
        """Kept for callers of the first version; reads PDFs, CSV and Excel alike."""
        return self.import_files(files)

    def import_files(self, files: Sequence[Tuple[str, bytes]]) -> ImportResult:
        """Trade confirmation PDFs and order spreadsheets (CSV, Excel), any number at once."""
        if not files:
            raise PortfolioError("choose one or more confirmation PDFs or order spreadsheets")
        if len(files) > MAX_FILES:
            raise PortfolioError(f"upload at most {MAX_FILES} files at a time")
        existing = read_executions(self.ledger_path)
        new_rows = []
        results: List[FileResult] = []
        for name, data in files:
            res = FileResult(name=Path(name or "upload.pdf").name[:120])
            results.append(res)
            if not data or len(data) > MAX_PDF_BYTES:
                res.error = "empty or larger than 10 MB"
                continue
            suffix = Path(res.name).suffix.lower()
            try:
                if data.startswith(b"%PDF"):
                    executions, report = parse_confirmation_pdf(io.BytesIO(data), name=res.name)
                    notes = report.warnings + [f"not read: {line}" for line in report.unreadable_lines]
                elif suffix in (".csv", ".txt"):
                    executions, notes = executions_from_table(_read_csv(data), source=res.name)
                elif suffix in (".xlsx", ".xlsm") and data.startswith(b"PK"):
                    import pandas as pd

                    executions, notes = executions_from_table(pd.read_excel(io.BytesIO(data)), source=res.name)
                else:
                    res.error = "not a PDF, CSV or Excel (.xlsx) file"
                    continue
            except (ConfirmationParseError, OrdersTableError) as exc:
                res.error = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - a broken spreadsheet is reported, not fatal
                res.error = f"cannot read the file ({type(exc).__name__})"
                continue
            if not executions:
                res.error = "no orders found in this file" + (f" ({notes[0]})" if notes else "")
                continue
            res.trades = len(executions)
            res.warnings = notes
            added, dupes = merge_new(existing + new_rows, executions)
            res.added, res.duplicates = len(added), dupes
            new_rows.extend(added)
        result = ImportResult(files=results, added=sum(r.added for r in results),
                              duplicates=sum(r.duplicates for r in results),
                              at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if new_rows:
            write_executions(self.ledger_path, existing + new_rows)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(json.dumps({
            "at": result.at, "added": result.added, "duplicates": result.duplicates,
            "files": [r.__dict__ for r in results]}, indent=2), encoding="utf-8")
        return result

    def last_import(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def clear(self) -> Optional[Path]:
        """Start over: the ledger is moved to a dated backup, never deleted outright."""
        if not self.ledger_path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = self.ledger_path.with_name(f"executions.backup-{stamp}.csv")
        shutil.move(str(self.ledger_path), str(backup))
        self.report_path.unlink(missing_ok=True)
        return backup

    def write_holdings(self, dest: Path) -> int:
        """Replace the monitor's holdings file (option B) with the open positions; backs up the old one."""
        view = self.view()
        if not view.open_operations:
            raise PortfolioError("no open positions to write: import your confirmations first")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            shutil.copyfile(dest, dest.with_name(f"{dest.stem}.backup-{stamp}{dest.suffix}"))
        dest.write_text(holdings_csv_text(view), encoding="utf-8")
        return len(view.open_operations)

    # ---------------------------------------------------------------- prices
    def quotes(self, view: Optional[LedgerView] = None, *, live: bool = True) -> Dict[str, Dict[str, Any]]:
        """Latest price per ledger symbol for the open positions (at most 5 minutes old when live)."""
        view = view or self.view()
        symbols = {s: normalize_symbol(s) for s in view.open_symbols}
        got = (self.market.quotes if live else self.market.cached_quotes)(list(symbols.values()))
        return {s: got[y] for s, y in symbols.items() if y in got}

    # --------------------------------------------------------------- outlook
    OUTLOOK_VERSION = 3
    OUTLOOK_HISTORY_DAYS = 12 * 365         # ~10 years of monthly tests: enough to judge even a 1-year trend

    def _long_bars(self, symbol: str):
        return self.market.bars(normalize_symbol(symbol), start=date.today() - timedelta(days=self.OUTLOOK_HISTORY_DAYS))

    def outlooks(self, view: Optional[LedgerView] = None) -> Dict[str, Dict[str, Any]]:
        """1/3/6/12-month outlook per open position (ledger symbol), computed once per trading day."""
        view = view or self.view()
        out: Dict[str, Dict[str, Any]] = {}
        for symbol in view.open_symbols:
            y = normalize_symbol(symbol)
            try:
                bars = self._long_bars(symbol)
            except MarketDataError as exc:
                out[symbol] = {"status": "error", "error": str(exc)}
                continue
            as_of = bars.index[-1].strftime("%Y-%m-%d")
            path = self.market.cache_dir / "outlook" / f"{y}.json"
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
            if cached and cached.get("as_of") == as_of and cached.get("version") == self.OUTLOOK_VERSION:
                out[symbol] = cached
                continue
            result = {**stock_outlook(bars, key=y), "version": self.OUTLOOK_VERSION}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result), encoding="utf-8")
            out[symbol] = result
        return out

    def commission_rate(self, view: LedgerView) -> float:
        """What you have actually paid in commission and fees, as a share of the amount traded."""
        traded = sum(o.principal for o in view.orders)
        return (sum(o.commission + o.fees for o in view.orders) / traded) if traded else 0.0

    def prognoses(self, view: LedgerView, outlooks: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Main verdict per open lot (wait / likely to fall / no reliable direction), keyed ``SYMBOL|buy_order``."""
        rate = self.commission_rate(view)
        return {f"{op.symbol}|{op.buy_order}": prognosis(outlooks.get(op.symbol), commission_rate=rate,
                                                         cost_per_share=(op.cost or 0.0) / op.shares)
                for op in view.open_operations if op.shares}

    def exit_plans(self, view: LedgerView, outlooks: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """The best 1-month exit plan per open lot, keyed ``SYMBOL|buy_order``."""
        rate = self.commission_rate(view)
        plans: Dict[str, Dict[str, Any]] = {}
        paths_by_symbol: Dict[str, Any] = {}
        for op in view.open_operations:
            ol = outlooks.get(op.symbol) or {}
            if ol.get("status") != "ok" or not op.shares:
                continue
            validated = bool(((ol.get("horizons") or {}).get("21") or {}).get("validated"))
            if op.symbol not in paths_by_symbol:
                try:
                    paths_by_symbol[op.symbol] = month_paths(self._long_bars(op.symbol),
                                                             key=normalize_symbol(op.symbol), with_trend=validated)
                except MarketDataError:
                    paths_by_symbol[op.symbol] = None
            paths = paths_by_symbol[op.symbol]
            if paths is None:
                continue
            plans[f"{op.symbol}|{op.buy_order}"] = exit_plan(
                paths, price=ol["last_close"], cost_per_share=(op.cost or 0.0) / op.shares, shares=op.shares,
                commission_rate=rate, trend_used=validated)
        return plans

    # ------------------------------------------------------------------ rows
    def rows(self, view: LedgerView, quotes: Dict[str, Dict[str, Any]],
             outlooks: Optional[Dict[str, Dict[str, Any]]] = None,
             plans: Optional[Dict[str, Dict[str, Any]]] = None,
             prognoses: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        names = {s: (self.market.cached_meta(s).get("name") or None) for s in {o.symbol for o in view.operations}}
        out = []
        for op in view.operations:
            row: Dict[str, Any] = {
                "symbol": op.symbol, "name": names.get(op.symbol) or op.name, "status": op.status,
                "shares": op.shares, "currency": op.currency,
                "buy_date": _iso(op.buy_date), "buy_price": op.buy_price, "cost": op.cost,
                "sell_date": _iso(op.sell_date), "sell_price": op.sell_price, "proceeds": op.proceeds,
                "current_price": None, "price_as_of": None, "market_value": None,
                "pl": op.realized_pl, "return_pct": op.realized_return_pct,
                "holding_days": op.holding_days, "buy_order": op.buy_order, "sell_order": op.sell_order,
                "note": op.note,
            }
            if op.status == "OPEN":
                q = quotes.get(op.symbol) or {}
                price = q.get("price")
                if price is not None:
                    value = price * op.shares
                    row.update(current_price=price, price_as_of=q.get("as_of"), market_value=value,
                               price_stale=bool(q.get("stale")),
                               pl=value - (op.cost or 0.0),
                               return_pct=((value / op.cost - 1) * 100) if op.cost else None)
                else:
                    row["pl"] = row["return_pct"] = None
                if outlooks and op.symbol in outlooks:
                    row["outlook"] = outlooks[op.symbol]
                if plans and f"{op.symbol}|{op.buy_order}" in plans:
                    row["plan"] = plans[f"{op.symbol}|{op.buy_order}"]
                if prognoses and f"{op.symbol}|{op.buy_order}" in prognoses:
                    row["prognosis"] = prognoses[f"{op.symbol}|{op.buy_order}"]
            out.append(row)
        return out

    def summary(self, view: LedgerView, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        open_rows = [r for r in rows if r["status"] == "OPEN"]
        closed = [r for r in rows if r["status"] == "CLOSED"]
        priced = [r for r in open_rows if r["market_value"] is not None]
        cost_open = sum(r["cost"] or 0 for r in open_rows)
        value = sum(r["market_value"] for r in priced)
        unreal = sum(r["pl"] for r in priced)
        realized = sum(r["pl"] or 0 for r in closed)
        dates = [o.trade_date for o in view.orders]
        return {
            "currency": (view.orders[0].currency if view.orders else "USD"),
            "open_positions": len(view.open_symbols), "open_lots": len(open_rows),
            "stale_prices": sum(1 for r in open_rows if r.get("price_stale")),
            "cost_open": cost_open, "market_value": value if priced else None,
            "unpriced": len(open_rows) - len(priced),
            "unrealized_pl": unreal if priced else None,
            "unrealized_pct": (unreal / sum(r["cost"] or 0 for r in priced) * 100) if priced else None,
            "realized_pl": realized, "closed_trades": len(closed),
            "wins": sum(1 for r in closed if (r["pl"] or 0) > 0), "losses": sum(1 for r in closed if (r["pl"] or 0) < 0),
            "total_pl": realized + (unreal if priced else 0.0),
            "commissions": sum(o.commission + o.fees for o in view.orders),
            "orders": len(view.orders), "executions": len(view.executions),
            "first_trade": _iso(min(dates)) if dates else None, "last_trade": _iso(max(dates)) if dates else None,
        }

    def chart_data(self, view: Optional[LedgerView] = None) -> Dict[str, Any]:
        """What the portfolio charts need: symbols, first purchase, buy/sell markers, average cost."""
        view = view or self.view()
        symbols, since, markers, avg = [], {}, {}, {}
        for s in view.symbols:
            y = normalize_symbol(s)
            symbols.append(y)
            mine = [o for o in view.orders if o.symbol == s]
            since[y] = min(o.trade_date for o in mine).isoformat()
            markers[y] = [{"time": o.trade_date.isoformat(), "side": o.side, "shares": round(o.quantity, 8),
                           "price": o.price} for o in mine]
            lots = [op for op in view.open_operations if op.symbol == s]
            shares = sum(op.shares for op in lots)
            if shares:
                avg[y] = sum(op.cost or 0 for op in lots) / shares
        return {"symbols": symbols, "since": since, "markers": markers, "avg_cost": avg,
                "open": [normalize_symbol(s) for s in view.open_symbols]}

    # ------------------------------------------------------------------- CSV
    def operations_csv(self, rows: List[Dict[str, Any]]) -> str:
        cols = ["symbol", "name", "status", "shares", "buy_date", "buy_price", "cost", "sell_date", "sell_price",
                "proceeds", "current_price", "price_as_of", "market_value", "pl", "return_pct", "holding_days",
                "currency", "buy_order", "sell_order", "note"]
        outlook_cols = [f"{k}_{label}" for label in HORIZONS.values()
                        for k in ("trend", "p_up", "expected", "average", "min", "max", "trend_hits")]
        prognosis_cols = ["prognosis", "prognosis_basis", "expected_price", "expected_date", "expected_vs_cost_pct",
                          "break_even", "break_even_date", "chance_of_halving_1y"]
        plan_cols = ["recommendation", "rec_limit", "rec_fill_chance", "rec_exit_vs_cost_pct", "rec_stop",
                     "rec_stop_chance", "rec_now_vs_cost_pct", "rec_reason", "rec_confidence", "outlook_as_of"]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols + prognosis_cols + outlook_cols + plan_cols)
        for r in rows:
            ol = r.get("outlook") or {}
            hz = ol.get("horizons") or {}
            pg = r.get("prognosis") or {}
            extra: List[Any] = [pg.get("headline"), pg.get("basis"), pg.get("expected"), pg.get("expected_date"),
                                pg.get("expected_vs_cost_pct"), pg.get("break_even"), pg.get("break_even_date"),
                                pg.get("p_halve_1y")]
            for h in HORIZONS:
                x = hz.get(str(h)) or {}
                extra += [x.get("trend"), x.get("p_up"), x.get("median"), x.get("average"), x.get("min"),
                          x.get("max"), f"{x['hits']}/{x['tests']}" if x.get("tests") else None]
            plan = r.get("plan") or {}
            extra += [_plan_text(plan) if plan else None, plan.get("limit"), plan.get("p_fill"),
                      plan.get("exit_vs_cost_pct"), plan.get("stop"), plan.get("p_stop"),
                      plan.get("now_vs_cost_pct"), plan.get("why"), plan.get("confidence"), ol.get("as_of")]
            w.writerow([csv_safe(_cell(r.get(c))) for c in cols] + [csv_safe(_cell(v)) for v in extra])
        return buf.getvalue()

    def executions_csv(self) -> str:
        if not self.ledger_path.exists():
            return ""
        return self.ledger_path.read_text(encoding="utf-8")


def _plan_text(plan: Dict[str, Any]) -> str:
    if plan.get("plan") == "sell_now":
        return f"sell now at about {plan['price']:.4g}"
    text = f"limit sell at {plan['limit']:.4g}"
    return text + (f" with stop at {plan['stop']:.4g}" if plan.get("stop") is not None else "")


def _read_csv(data: bytes):
    """A CSV export in UTF-8 (with or without BOM) or Latin-1, comma or semicolon separated."""
    import pandas as pd

    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sep = ";" if text.splitlines()[0].count(";") > text.splitlines()[0].count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _cell(v: Any) -> Any:
    if isinstance(v, float):
        return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:.4f}".rstrip("0").rstrip(".")
    return "" if v is None else v


__all__ = ["BUY", "FileResult", "PortfolioError", "ImportResult", "Portfolio"]
