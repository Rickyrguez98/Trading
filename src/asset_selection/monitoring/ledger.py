"""Your trade history: executions -> orders -> matched buys and sells.

A broker confirmation lists *executions*. GBM's US-dollar account (cleared by
DriveWealth) fills one order as up to two executions at the same price and
second -- the whole shares and the fractional remainder -- so executions are
first grouped back into the *orders* you placed. Sells are then matched to
earlier buys **first in, first out**, per symbol, which turns the history into
one row per buy lot: when and at what price it was bought, and either when
and at what price it was sold, or that it is still open.

Costs include the commission paid on each side, split pro rata by shares, so
a row's result is what actually reached your account. A sale that cannot be
matched to a known buy (its purchase confirmation was not imported) is kept
and flagged, never silently dropped, and a few millionths of a share left
over by fractional rounding are reported as a residue rather than shown as a
position.

Pure Python + pandas: no network, no PDF code (see ``confirmations.py``).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .models import Lot

BUY, SELL = "BUY", "SELL"
DUST_SHARES = 1e-3          # less than a thousandth of a share left after a sale is rounding residue
GROUP_SECONDS = 120         # executions of one order arrive within a second or two


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Execution:
    """One fill as printed on a confirmation. Amounts are positive numbers."""

    symbol: str
    side: str                         # BUY | SELL
    quantity: float
    price: float
    trade_date: date
    exec_time: Optional[str] = None   # "10:16:52 AM" as printed (US Eastern)
    settle_date: Optional[date] = None
    description: str = ""
    principal: float = 0.0            # quantity x price as booked
    commission: float = 0.0
    fees: float = 0.0                 # transaction fee + other fees / credits
    net_amount: float = 0.0           # cash paid for a buy, received for a sell
    currency: str = "USD"
    capacity: Optional[str] = None
    account: Optional[str] = None     # masked, e.g. "…0740"
    broker: str = "GBM (DriveWealth)"
    source: str = ""                  # "Confirm_….pdf p1"

    @property
    def key(self) -> Tuple:
        """Identity used to recognise the same execution imported twice."""
        return (self.symbol, self.side, self.trade_date.isoformat(), self.exec_time or "",
                round(self.quantity, 8), round(self.price, 6))

    @property
    def seconds(self) -> int:
        return _seconds(self.exec_time)


@dataclass
class Order:
    """Executions of one order, merged."""

    id: str
    symbol: str
    side: str
    trade_date: date
    exec_time: Optional[str]
    quantity: float
    price: float                      # share-weighted average
    principal: float
    commission: float
    fees: float
    net_amount: float
    description: str
    currency: str
    executions: int = 1
    sources: List[str] = field(default_factory=list)

    @property
    def unit_cash(self) -> float:
        """Cash per share including commission and fees (paid on a buy, received on a sell)."""
        return self.net_amount / self.quantity if self.quantity else 0.0


@dataclass
class Operation:
    """One row of the history: a buy lot and what became of it."""

    symbol: str
    name: str
    status: str                       # CLOSED | OPEN | UNMATCHED_SELL
    shares: float
    currency: str
    buy_order: Optional[str] = None
    buy_date: Optional[date] = None
    buy_price: Optional[float] = None
    cost: Optional[float] = None      # paid, incl. buy commission (pro rata)
    sell_order: Optional[str] = None
    sell_date: Optional[date] = None
    sell_price: Optional[float] = None
    proceeds: Optional[float] = None  # received, net of sell commission (pro rata)
    realized_pl: Optional[float] = None
    realized_return_pct: Optional[float] = None
    holding_days: Optional[int] = None
    note: str = ""


@dataclass
class LedgerView:
    executions: List[Execution]
    orders: List[Order]
    operations: List[Operation]
    residues: Dict[str, float] = field(default_factory=dict)      # symbol -> shares
    warnings: List[str] = field(default_factory=list)

    @property
    def open_operations(self) -> List[Operation]:
        return [o for o in self.operations if o.status == "OPEN"]

    @property
    def symbols(self) -> List[str]:
        """Every traded symbol: open positions first, then the rest, each by first purchase."""
        first: Dict[str, Tuple[int, date]] = {}
        for op in self.operations:
            d = op.buy_date or op.sell_date or date.max
            rank = 0 if op.status == "OPEN" else 1
            cur = first.get(op.symbol)
            if cur is None or (rank, d) < cur:
                first[op.symbol] = (rank, d)
        return [s for s, _ in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))]

    @property
    def open_symbols(self) -> List[str]:
        return [s for s in self.symbols if any(o.symbol == s for o in self.open_operations)]

    def realized_total(self) -> float:
        return sum(o.realized_pl or 0.0 for o in self.operations if o.status == "CLOSED")

    def open_cost(self) -> float:
        return sum(o.cost or 0.0 for o in self.open_operations)


# ---------------------------------------------------------------------------
# Orders and FIFO matching
# ---------------------------------------------------------------------------

def group_orders(executions: Iterable[Execution]) -> List[Order]:
    """Merge executions of the same order (same symbol, side, day and price, seconds apart)."""
    ordered = sorted(executions, key=lambda e: (e.trade_date, e.seconds, e.symbol, e.side, e.price))
    orders: List[Order] = []
    open_by_key: Dict[Tuple, Order] = {}
    last_seen: Dict[Tuple, int] = {}
    counter: Counter = Counter()
    for e in ordered:
        k = (e.symbol, e.side, e.trade_date, round(e.price, 6))
        o = open_by_key.get(k)
        if o is not None and abs(e.seconds - last_seen[k]) <= GROUP_SECONDS:
            o.principal += e.principal
            o.commission += e.commission
            o.fees += e.fees
            o.net_amount += e.net_amount
            o.price = round((o.price * o.quantity + e.price * e.quantity) / (o.quantity + e.quantity), 6)
            o.quantity += e.quantity
            o.executions += 1
            if e.source and e.source not in o.sources:
                o.sources.append(e.source)
        else:
            counter[(e.symbol, e.trade_date)] += 1
            o = Order(
                id=f"{e.symbol}-{e.trade_date.isoformat()}-{e.side[0]}{counter[(e.symbol, e.trade_date)]}",
                symbol=e.symbol, side=e.side, trade_date=e.trade_date, exec_time=e.exec_time,
                quantity=e.quantity, price=e.price, principal=e.principal, commission=e.commission,
                fees=e.fees, net_amount=e.net_amount, description=e.description, currency=e.currency,
                sources=[e.source] if e.source else [],
            )
            orders.append(o)
            open_by_key[k] = o
        last_seen[k] = e.seconds
    return orders


@dataclass
class _OpenLot:
    order: Order
    remaining: float


def consolidate(executions: Sequence[Execution]) -> LedgerView:
    """Orders, FIFO-matched operations, residues and warnings for a set of executions."""
    orders = group_orders(executions)
    by_symbol: Dict[str, List[Order]] = defaultdict(list)
    for o in orders:
        by_symbol[o.symbol].append(o)

    operations: List[Operation] = []
    residues: Dict[str, float] = {}
    warnings: List[str] = []
    for symbol in sorted(by_symbol):
        # Buys before sells on the same day and second: a same-day round trip matches itself.
        seq = sorted(by_symbol[symbol], key=lambda o: (o.trade_date, _seconds(o.exec_time), o.side != BUY))
        name = security_name(seq[0].description) or symbol
        lots: List[_OpenLot] = []
        for o in seq:
            if o.side == BUY:
                lots.append(_OpenLot(o, o.quantity))
                continue
            to_sell = o.quantity
            while to_sell > 1e-12 and lots:
                lot = lots[0]
                q = min(lot.remaining, to_sell)
                operations.append(_closed(symbol, name, lot.order, o, q))
                lot.remaining -= q
                to_sell -= q
                if lot.remaining <= 1e-12:
                    lots.pop(0)
            if to_sell > DUST_SHARES:
                share = to_sell / o.quantity
                operations.append(Operation(
                    symbol=symbol, name=name, status="UNMATCHED_SELL", shares=to_sell, currency=o.currency,
                    sell_order=o.id, sell_date=o.trade_date, sell_price=o.price,
                    proceeds=o.net_amount * share,
                    note="sold shares bought before the first imported confirmation: import that purchase",
                ))
                warnings.append(
                    f"{symbol}: the sale on {o.trade_date.isoformat()} exceeds the known purchases by "
                    f"{to_sell:.8f} shares; import the confirmation of the original purchase"
                )
        for lot in lots:
            if lot.remaining > DUST_SHARES:
                operations.append(_open(symbol, name, lot.order, lot.remaining))
            elif lot.remaining > 1e-12:
                residues[symbol] = residues.get(symbol, 0.0) + lot.remaining
    for symbol, shares in sorted(residues.items()):
        warnings.append(
            f"{symbol}: {shares:.8f} shares remain after the sale (fractional rounding residue); "
            "not shown as an open position"
        )
    operations.sort(key=lambda op: (op.symbol, op.buy_date or op.sell_date or date.min,
                                    op.sell_date or date.max))
    return LedgerView(executions=list(executions), orders=orders, operations=operations,
                      residues=residues, warnings=warnings)


def _closed(symbol: str, name: str, buy: Order, sell: Order, q: float) -> Operation:
    cost = buy.unit_cash * q
    proceeds = sell.unit_cash * q
    pl = proceeds - cost
    return Operation(
        symbol=symbol, name=name, status="CLOSED", shares=q, currency=buy.currency,
        buy_order=buy.id, buy_date=buy.trade_date, buy_price=buy.price, cost=cost,
        sell_order=sell.id, sell_date=sell.trade_date, sell_price=sell.price, proceeds=proceeds,
        realized_pl=pl, realized_return_pct=(pl / cost * 100.0) if cost else None,
        holding_days=(sell.trade_date - buy.trade_date).days,
    )


def _open(symbol: str, name: str, buy: Order, q: float) -> Operation:
    return Operation(
        symbol=symbol, name=name, status="OPEN", shares=q, currency=buy.currency,
        buy_order=buy.id, buy_date=buy.trade_date, buy_price=buy.price, cost=buy.unit_cash * q,
        holding_days=(date.today() - buy.trade_date).days,
    )


# ---------------------------------------------------------------------------
# Holdings for the monitor
# ---------------------------------------------------------------------------

def open_lots(view: LedgerView, *, broker: str = "GBM (DriveWealth)") -> List[Lot]:
    """Open positions as monitor lots (one per buy lot, cost incl. commission)."""
    return [
        Lot(ticker=op.symbol, shares=op.shares, unit_cost=(op.cost or 0.0) / op.shares, currency=op.currency,
            market="US", purchase_date=op.buy_date, broker=broker, source=f"confirmation {op.buy_order}")
        for op in view.open_operations
    ]


HOLDINGS_HEADER = ["ticker", "shares", "avg_cost", "currency", "market", "purchase_date", "broker", "notes"]


def holdings_csv_text(view: LedgerView, *, broker: str = "GBM (DriveWealth)") -> str:
    """The open positions in the holdings-template format the monitor reads (option B)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HOLDINGS_HEADER)
    for lot in open_lots(view, broker=broker):
        w.writerow([lot.ticker, f"{lot.shares:.8f}", f"{lot.unit_cost:.6f}", lot.currency, lot.market,
                    lot.purchase_date.isoformat() if lot.purchase_date else "", lot.broker,
                    f"from trade confirmations ({lot.source})"])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Ledger file (CSV) -- what the app keeps between imports
# ---------------------------------------------------------------------------

_FIELDS = [f.name for f in fields(Execution)]


def write_executions(path: Union[str, Path], executions: Iterable[Execution]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_FIELDS, lineterminator="\n")
        w.writeheader()
        for e in sorted(executions, key=lambda e: (e.trade_date, e.seconds, e.symbol)):
            row = asdict(e)
            for k in ("trade_date", "settle_date"):
                row[k] = row[k].isoformat() if row[k] else ""
            w.writerow({k: csv_safe(v) for k, v in row.items()})
    tmp.replace(p)


def read_executions(path: Union[str, Path]) -> List[Execution]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(Execution(
                symbol=row["symbol"], side=row["side"], quantity=float(row["quantity"]), price=float(row["price"]),
                trade_date=date.fromisoformat(row["trade_date"]), exec_time=row.get("exec_time") or None,
                settle_date=date.fromisoformat(row["settle_date"]) if row.get("settle_date") else None,
                description=_unsafe(row.get("description", "")), principal=float(row.get("principal") or 0),
                commission=float(row.get("commission") or 0), fees=float(row.get("fees") or 0),
                net_amount=float(row.get("net_amount") or 0), currency=row.get("currency") or "USD",
                capacity=row.get("capacity") or None, account=row.get("account") or None,
                broker=row.get("broker") or "GBM (DriveWealth)", source=_unsafe(row.get("source", "")),
            ))
    return out


def merge_new(existing: Sequence[Execution], incoming: Sequence[Execution]) -> Tuple[List[Execution], int]:
    """Add executions not already in the ledger; returns (added, duplicates skipped).

    Counts are compared per identity, so re-importing a file adds nothing while
    two genuinely identical fills on one confirmation are both kept.
    """
    have = Counter(e.key for e in existing)
    seen: Counter = Counter()
    added, dupes = [], 0
    for e in incoming:
        seen[e.key] += 1
        if seen[e.key] <= have[e.key]:
            dupes += 1
        else:
            added.append(e)
    return added, dupes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUFFIXES = re.compile(
    r"\s+(COM(MON)?( STOCK| STK)?( NEW| CL [A-C]| SUB VTG)?|CAP STK( CL [A-C])?|CL(ASS)? [A-C]( COM)?|"
    r"SHS( BEN INT)?|ORDINARY SHARES|ETF|NEW|GOLD SHS)$"
)


def security_name(description: str) -> str:
    """``NVIDIA CORPORATION COM`` -> ``Nvidia Corporation``; ``VISTRA CORP COM`` -> ``Vistra Corp``."""
    name = re.sub(r"\s+", " ", str(description or "").strip().upper())
    for _ in range(4):
        stripped = _SUFFIXES.sub("", name)
        if stripped == name:
            break
        name = stripped
    return " ".join(w if (len(w) <= 4 and w.isalpha() and w in _ACRONYMS) else w.capitalize()
                    for w in name.split())


_ACRONYMS = {"AST", "BWX", "SPDR", "IREN", "EOS", "AMD", "IBM", "ETF", "PWR", "AI", "USA", "II", "III"}


def _seconds(exec_time: Optional[str]) -> int:
    if not exec_time:
        return 0
    try:
        t = datetime.strptime(exec_time.strip().upper(), "%I:%M:%S %p")
    except ValueError:
        return 0
    return t.hour * 3600 + t.minute * 60 + t.second


def csv_safe(value):
    """Keep spreadsheet apps from treating a text cell as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + value
    return value


def _unsafe(value: str) -> str:
    return value[1:] if value[:2] in ("'=", "'+", "'@") else value
