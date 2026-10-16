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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asset_selection.monitoring.confirmations import ConfirmationParseError, parse_confirmation_pdf
from asset_selection.monitoring.ledger import (
    BUY,
    LedgerView,
    consolidate,
    csv_safe,
    holdings_csv_text,
    merge_new,
    read_executions,
    write_executions,
)

from .market import MarketData, normalize_symbol
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
        if not files:
            raise PortfolioError("choose one or more confirmation PDFs")
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
            if not data.startswith(b"%PDF"):
                res.error = "not a PDF"
                continue
            try:
                executions, report = parse_confirmation_pdf(io.BytesIO(data), name=res.name)
            except ConfirmationParseError as exc:
                res.error = str(exc)
                continue
            res.trades = len(executions)
            res.warnings = report.warnings + [f"not read: {line}" for line in report.unreadable_lines]
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

    # ------------------------------------------------------------------ rows
    def rows(self, view: LedgerView, quotes: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
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
                               pl=value - (op.cost or 0.0),
                               return_pct=((value / op.cost - 1) * 100) if op.cost else None)
                else:
                    row["pl"] = row["return_pct"] = None
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
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([csv_safe(_cell(r.get(c))) for c in cols])
        return buf.getvalue()

    def executions_csv(self) -> str:
        if not self.ledger_path.exists():
            return ""
        return self.ledger_path.read_text(encoding="utf-8")


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _cell(v: Any) -> Any:
    if isinstance(v, float):
        return f"{v:.8f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:.4f}".rstrip("0").rstrip(".")
    return "" if v is None else v


__all__ = ["BUY", "FileResult", "PortfolioError", "ImportResult", "Portfolio"]
