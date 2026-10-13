"""Extract holdings from a brokerage statement PDF (GBM, DriveWealth, generic).

Design rules -- what this parser will and will not do:

* The **holdings table is authoritative** for share counts and cost. Nothing
  is inferred from market value or price columns, and a row without a
  positive cost is skipped, never filled in.
* **Purchase dates** come only from buy movements, and only when those buys
  fully explain the position (their shares sum to the holding). A monthly
  statement lists that month's movements; using a partial set would assign a
  wrong first-purchase date, so in that case the date is left blank and the
  report says so.
* No holdings table, no result. The parser refuses rather than rebuilding a
  position from one month of movements.
* Everything extracted is written to a review CSV in the template format, so
  the user can audit and correct it, then re-run with ``--holdings`` on it.

Layout calibration: the column vocabulary covers GBM's Spanish statement terms
(Emisora, Serie, Títulos, Costo promedio, ...) and DriveWealth's English ones,
but the parser has been verified only against synthetic statements in the test
suite. A real GBM statement may use a layout it does not anticipate, and the
extraction report is written to make that visible rather than hide it.

Requires the optional ``pdfplumber`` dependency (``pip install '.[pdf]'``).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .holdings import (
    HoldingsError,
    canonical_field,
    is_non_instrument,
    normalize_header,
    parse_date,
    parse_number,
    resolve_instrument,
)
from .models import Lot

logger = logging.getLogger(__name__)

_BUY = re.compile(r"\b(compra|buy|bought|purchase|adquisicion)\b", re.IGNORECASE)
_SELL = re.compile(r"\b(venta|sell|sold|enajenacion)\b", re.IGNORECASE)

# Section headings that tell us which market a table belongs to.
_SIC_HINT = re.compile(r"\b(sic|sistema internacional|mercado global|internacional)\b")
_BMV_HINT = re.compile(r"\b(bmv|biva|mercado nacional|mercado local|acciones nacionales)\b")

_COST_MISMATCH_TOLERANCE = 0.02


class StatementParseError(HoldingsError):
    """The PDF does not contain a holdings table this parser can read."""


@dataclass
class TableInfo:
    page: int
    index: int
    kind: str                                   # holdings | transactions | other
    strategy: str                               # lines | text
    columns: Dict[str, str]                     # canonical -> raw header
    rows: int
    market_hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ExtractionReport:
    source: str
    statement_type: str = "generic"             # gbm | drivewealth | generic
    pages: int = 0
    tables: List[TableInfo] = field(default_factory=list)
    holdings_rows: int = 0
    buy_rows: int = 0
    sell_rows: int = 0
    lots: int = 0
    dates_attached: int = 0
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    confidence: str = "low"                     # high | medium | low
    review_csv: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "statement_type": self.statement_type,
            "pages": self.pages,
            "tables": [t.to_dict() for t in self.tables],
            "holdings_rows": self.holdings_rows,
            "buy_rows": self.buy_rows,
            "sell_rows": self.sell_rows,
            "lots": self.lots,
            "dates_attached": self.dates_attached,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "confidence": self.confidence,
            "review_csv": self.review_csv,
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_holdings_from_pdf(
    path: Union[str, Path],
    *,
    us_universe: Optional[Set[str]] = None,
) -> Tuple[List[Lot], ExtractionReport]:
    """Parse a statement PDF into lots plus a report of how they were obtained.

    ``us_universe`` (canonical US tickers) helps decide whether an MXN position
    without a market heading is an SIC (US) or BMV (Mexican) listing.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise StatementParseError(
            "reading PDF statements needs pdfplumber: pip install '.[pdf]'"
        ) from exc

    p = Path(path)
    if not p.exists():
        raise StatementParseError(f"statement not found: {p}")
    report = ExtractionReport(source=str(p))

    page_tables: List[Tuple[int, int, str, List[List[Optional[str]]], str]] = []
    full_text: List[str] = []
    try:
        with pdfplumber.open(str(p)) as pdf:
            report.pages = len(pdf.pages)
            for page_no, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                full_text.append(text)
                found, strategy = _page_tables(page)
                for idx, (rows, bbox) in enumerate(found):
                    page_tables.append((page_no, idx, strategy, rows, _heading_above(page, bbox, text)))
    except StatementParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the real cause
        raise StatementParseError(f"could not read {p.name}: {type(exc).__name__}: {exc}") from exc

    report.statement_type = _statement_type("\n".join(full_text))
    default_ccy, default_mkt = _defaults_for(report.statement_type)

    holdings_rows: List[Dict[str, Any]] = []
    movements: List[Dict[str, Any]] = []
    last_holdings: Optional[Tuple[int, int, Dict[int, str], Optional[str]]] = None

    for page_no, idx, strategy, table, heading in page_tables:
        header_at, colmap = _find_header(table)
        kind = _classify(colmap)
        hint = _market_hint(heading)
        body = table[header_at + 1:] if header_at is not None else table

        if (kind == "other" and last_holdings is not None and table and idx == 0
                and page_no == last_holdings[0] + 1 and len(table[0]) == last_holdings[1]):
            # A header-less table at the top of the next page, with the same
            # shape as the holdings table that ended the previous page.
            kind, colmap, hint = "holdings", last_holdings[2], last_holdings[3]
            body = table

        info = TableInfo(
            page=page_no, index=idx, kind=kind, strategy=strategy,
            columns={f: _raw_header(table, header_at, ci) for ci, f in colmap.items()},
            rows=len(body), market_hint=hint,
        )
        report.tables.append(info)
        if kind == "holdings":
            last_holdings = (page_no, len(table[0]) if table else 0, colmap, hint)
            for r_i, row in enumerate(body):
                rec = _record(row, colmap)
                rec["_where"] = f"{p.name} p{page_no} table{idx + 1} row{r_i + 1}"
                rec["_hint"] = hint
                holdings_rows.append(rec)
        elif kind == "transactions":
            for r_i, row in enumerate(body):
                rec = _record(row, colmap)
                rec["_where"] = f"{p.name} p{page_no} table{idx + 1} row{r_i + 1}"
                rec["_hint"] = hint
                movements.append(rec)

    if not holdings_rows:
        kinds = sorted({t.kind for t in report.tables}) or ["none"]
        raise StatementParseError(
            f"{p.name}: no holdings table found (tables seen: {len(report.tables)}, kinds: "
            f"{', '.join(kinds)}). Fill configs/us_equities/holdings_template.csv instead, "
            "or share a redacted statement so the parser can be calibrated."
        )

    lots = _lots_from_holdings(holdings_rows, default_ccy, default_mkt, us_universe, report)
    buys = _parse_movements(movements, report)
    lots = _attach_dates(lots, buys, report)

    report.lots = len(lots)
    report.confidence = _confidence(report, lots)
    if not lots:
        raise StatementParseError(
            f"{p.name}: a holdings table was found but no row had a ticker, a positive share "
            f"count and a positive cost ({len(report.skipped)} rows skipped)"
        )
    return lots, report


# ---------------------------------------------------------------------------
# Table discovery
# ---------------------------------------------------------------------------

def _page_tables(page) -> Tuple[List[Tuple[List[List[Optional[str]]], Tuple[float, ...]]], str]:
    """Ruled tables first; fall back to whitespace-aligned text tables.

    Each table is returned with its own bounding box, so the section heading
    read above it always belongs to that table.
    """
    for strategy, settings in (
        ("lines", {}),
        ("text", {"vertical_strategy": "text", "horizontal_strategy": "text"}),
    ):
        found = []
        for t in page.find_tables(settings) or []:
            rows = t.extract()
            if _useful(rows):
                found.append((rows, tuple(t.bbox)))
        if found:
            return found, strategy
    return [], "none"


def _useful(table) -> bool:
    return bool(table) and len(table) >= 2 and max(len(r) for r in table) >= 3


def _heading_above(page, bbox: Tuple[float, ...], page_text: str) -> str:
    """Text in the band just above a table: its section heading, if any."""
    top = bbox[1]
    try:
        band = page.crop((0, max(0.0, top - 60.0), page.width, top))
        return band.extract_text() or ""
    except Exception:  # noqa: BLE001 - a heading is only a hint
        lines = page_text.splitlines()
        return " ".join(lines[:3])


def _find_header(table) -> Tuple[Optional[int], Dict[int, str]]:
    """First row (within the first 4) that names a ticker plus one more known field."""
    best: Tuple[Optional[int], Dict[int, str]] = (None, {})
    for r_i, row in enumerate(table[:4]):
        colmap: Dict[int, str] = {}
        for c_i, cell in enumerate(row):
            f = canonical_field(_clean(cell))
            if f and f not in colmap.values():
                colmap[c_i] = f
        if "ticker" in colmap.values() and len(colmap) >= 2 and len(colmap) > len(best[1]):
            best = (r_i, colmap)
    return best


def _classify(colmap: Dict[int, str]) -> str:
    fields = set(colmap.values())
    if {"ticker", "shares"} <= fields and "operation" in fields and "purchase_date" in fields:
        return "transactions"
    if {"ticker", "shares"} <= fields and ({"unit_cost", "total_cost"} & fields):
        return "holdings"
    return "other"


def _raw_header(table, header_at: Optional[int], ci: int) -> str:
    if header_at is None:
        return "(continuation)"
    try:
        return _clean(table[header_at][ci])
    except IndexError:
        return ""


def _record(row, colmap: Dict[int, str]) -> Dict[str, Any]:
    rec: Dict[str, Any] = {}
    for ci, f in colmap.items():
        rec[f] = _clean(row[ci]) if ci < len(row) else ""
    return rec


def _clean(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "")).strip()


def _statement_type(text: str) -> str:
    t = normalize_header(text)
    if "drivewealth" in t:
        return "drivewealth"
    if "gbm" in t and ("estado de cuenta" in t or "casa de bolsa" in t):
        return "gbm"
    return "generic"


def _defaults_for(statement_type: str) -> Tuple[Optional[str], Optional[str]]:
    if statement_type == "drivewealth":
        return "USD", "US"
    if statement_type == "gbm":
        return "MXN", None                       # SIC or BMV, decided per table/row
    return None, None


def _market_hint(heading: str) -> Optional[str]:
    """The market named by the heading line closest to the table, if any."""
    for line in reversed([ln for ln in heading.splitlines() if ln.strip()]):
        h = normalize_header(line)
        if _SIC_HINT.search(h):
            return "SIC"
        if _BMV_HINT.search(h):
            return "BMV"
    return None


# ---------------------------------------------------------------------------
# Rows -> lots
# ---------------------------------------------------------------------------

def _lots_from_holdings(
    rows: List[Dict[str, Any]],
    default_ccy: Optional[str],
    default_mkt: Optional[str],
    us_universe: Optional[Set[str]],
    report: ExtractionReport,
) -> List[Lot]:
    lots: List[Lot] = []
    for rec in rows:
        where = rec["_where"]
        raw = rec.get("ticker", "")
        if not raw or is_non_instrument(raw):
            continue
        report.holdings_rows += 1
        currency = _currency(rec.get("currency"), default_ccy)
        market = rec["_hint"] or (rec.get("market") or "").upper() or default_mkt
        inferred = False
        if market is None and currency == "MXN":
            market, inferred = _infer_market(raw, rec.get("series"), us_universe)
        try:
            inst = resolve_instrument(raw, market=market, currency=currency, series=rec.get("series"))
        except HoldingsError as exc:
            report.skipped.append(f"{where}: {exc}")
            continue

        shares = parse_number(rec.get("shares"))
        if shares is None or shares <= 0:
            report.skipped.append(f"{where}: {raw} has no positive share count")
            continue
        derived: List[str] = []
        unit_cost = parse_number(rec.get("unit_cost"))
        if unit_cost is None or unit_cost <= 0:
            total = parse_number(rec.get("total_cost"))
            if total is not None and total > 0:
                unit_cost, derived = total / shares, ["unit_cost"]
        if unit_cost is None or unit_cost <= 0:
            report.skipped.append(f"{where}: {raw} has no positive cost (never guessed)")
            continue

        notes = list(inst.warnings)
        if inferred:
            derived.append("market")
            notes.append(f"MARKET_INFERRED: {raw} treated as {inst.market} -- check the review CSV")
        for n in notes:
            report.warnings.append(f"{where}: {n}")
        lots.append(Lot(
            ticker=inst.ticker, shares=float(shares), unit_cost=float(unit_cost),
            currency=inst.currency, market=inst.market, purchase_date=None,
            broker=report.statement_type if report.statement_type != "generic" else None,
            source=where, raw_ticker=" ".join(x for x in (raw, rec.get("series") or "") if x),
            derived_fields=derived,
        ))
    return lots


def _infer_market(raw: str, series: Optional[str], us_universe: Optional[Set[str]]) -> Tuple[str, bool]:
    """An MXN line with no section heading: US listing (SIC) or Mexican (BMV)?"""
    root = re.split(r"[\s*]", raw.strip().upper())[0]
    s = (series or "").strip().upper()
    if us_universe is not None:
        return ("SIC" if root.replace("-", ".") in us_universe else "BMV"), True
    # Without a universe, a non-"*" series letter is the BMV convention (AMX B).
    return ("BMV" if s and s != "*" else "SIC"), True


def match_key(raw: object, series: object = None) -> str:
    """Broker spelling -> comparison key: 'GFNORTE O' / 'BRK.B' / 'NVDA *' -> GFNORTEO / BRKB / NVDA."""
    joined = f"{raw or ''}{series or ''}".upper().replace("*", "")
    key = re.sub(r"[^A-Z0-9]", "", joined)
    return key[:-2] if key.endswith("MX") and str(raw or "").upper().endswith(".MX") else key


def _currency(value: object, default: Optional[str]) -> Optional[str]:
    c = re.sub(r"[^A-Z$]", "", str(value or "").upper())
    if c in ("MXN", "MN", "MXP", "PESOS"):
        return "MXN"
    if c in ("USD", "DLS", "US", "USD$", "DOLARES"):
        return "USD"
    return default


@dataclass
class _Buy:
    key: str
    shares: float
    price: Optional[float]
    when: date
    where: str


def _parse_movements(rows: List[Dict[str, Any]], report: ExtractionReport) -> Dict[str, List[_Buy]]:
    buys: Dict[str, List[_Buy]] = defaultdict(list)
    sold: Set[str] = set()
    for rec in rows:
        op = rec.get("operation", "")
        raw = rec.get("ticker", "")
        if not raw or is_non_instrument(raw):
            continue
        is_buy, is_sell = bool(_BUY.search(op)), bool(_SELL.search(op))
        if not (is_buy or is_sell):
            continue
        key = match_key(raw, rec.get("series"))
        if is_sell:
            report.sell_rows += 1
            sold.add(key)
            continue
        shares = parse_number(rec.get("shares"))
        try:
            when = parse_date(rec.get("purchase_date"))
        except ValueError:
            when = None
        if shares is None or shares <= 0 or when is None:
            report.warnings.append(f"{rec['_where']}: buy movement without usable shares/date ignored")
            continue
        report.buy_rows += 1
        buys[key].append(_Buy(key=key, shares=float(shares), price=parse_number(rec.get("price")),
                              when=when, where=rec["_where"]))
    for key in sorted(sold):
        # Buys and sells in one period make the surviving lots' dates ambiguous.
        buys.pop(key, None)
        report.warnings.append(f"{key}: sells in this statement period; purchase dates not attached")
    return buys


def _attach_dates(lots: List[Lot], buys: Dict[str, List[_Buy]], report: ExtractionReport) -> List[Lot]:
    out: List[Lot] = []
    for lot in lots:
        mine = sorted(buys.get(match_key(lot.raw_ticker or lot.ticker)) or [], key=lambda b: b.when)
        if not mine:
            out.append(lot)
            continue
        bought = sum(b.shares for b in mine)
        if abs(bought - lot.shares) > 1e-6:
            report.warnings.append(
                f"{lot.ticker}: {len(mine)} buy(s) in this statement cover {bought:g} of "
                f"{lot.shares:g} shares; purchase date left blank (earlier buys are not in this "
                "statement) -- add it in the review CSV"
            )
            out.append(lot)
            continue
        # The buys fully explain the position: split it into dated lots whose
        # costs are scaled so the statement's cost basis stays authoritative.
        priced = [b for b in mine if b.price and b.price > 0]
        if len(priced) == len(mine):
            raw_cost = sum(b.shares * b.price for b in mine)
            scale = (lot.cost / raw_cost) if raw_cost > 0 else 1.0
            if abs(scale - 1.0) > _COST_MISMATCH_TOLERANCE:
                report.warnings.append(
                    f"{lot.ticker}: buy prices differ from the statement's average cost by "
                    f"{100 * (scale - 1):+.1f}% (commissions/fees?); lot costs scaled to the statement"
                )
            costs = [b.price * scale for b in mine]
        else:
            costs = [lot.unit_cost] * len(mine)
        for b, c in zip(mine, costs):
            out.append(Lot(
                ticker=lot.ticker, shares=b.shares, unit_cost=c, currency=lot.currency,
                market=lot.market, purchase_date=b.when, broker=lot.broker,
                source=f"{lot.source} + {b.where}", raw_ticker=lot.raw_ticker,
                derived_fields=lot.derived_fields + ["lot_split_from_movements"],
            ))
            report.dates_attached += 1
    return out


def _confidence(report: ExtractionReport, lots: Sequence[Lot]) -> str:
    if not lots:
        return "low"
    text_tables = any(t.strategy == "text" and t.kind == "holdings" for t in report.tables)
    derived = any(("unit_cost" in l.derived_fields or "market" in l.derived_fields) for l in lots)
    if report.skipped or text_tables:
        return "low"
    if derived or report.statement_type == "generic":
        return "medium"
    return "high"
