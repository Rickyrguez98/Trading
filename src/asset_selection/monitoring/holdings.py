"""Load the user's current holdings into :class:`Lot` / :class:`Holding` objects.

The CSV template (``configs/us_equities/holdings_template.csv``) is the
always-works input. PDF statements are parsed by :mod:`.pdf_statement`, which
reuses the header vocabulary, number parsing and date parsing defined here and
writes its result back out in this same CSV format so it can be audited.

Conventions, documented because they are where silent errors would hide:

* **Numbers** follow Mexican usage: ``,`` groups thousands and ``.`` marks
  decimals (``3,850.25``). A value carrying both separators takes whichever
  comes last as the decimal mark, so ``3.850,25`` also parses.
* **Dates** are ISO (``2025-03-14``) or day-first (``14/03/2025``,
  ``14-mar-2025``, ``14 MAR 25``), the Mexican convention. Month-first US dates
  are deliberately not accepted, because ``03/04/2025`` cannot be told apart.
* **Markets.** ``US`` = a US listing held in USD (e.g. GBM Trading USA);
  ``SIC`` = a US listing bought in MXN through the Sistema Internacional de
  Cotizaciones; ``BMV`` = a Mexican listing. SIC and BMV positions are carried
  in MXN, so their return includes the USDMXN move.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd

from ..data_providers.symbols import to_provider_symbol
from .models import Holding, Lot

logger = logging.getLogger(__name__)

TEMPLATE_COLUMNS = [
    "ticker", "shares", "avg_cost", "currency", "market", "purchase_date", "broker", "notes",
]

# ---------------------------------------------------------------------------
# Header vocabulary (Spanish + English). Keys are canonical field names.
# ---------------------------------------------------------------------------

HEADER_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "ticker": (
        "ticker", "symbol", "simbolo", "emisora", "emisora serie", "emisora y serie",
        "instrumento", "clave", "clave de pizarra", "pizarra", "security", "stock",
    ),
    "series": ("serie",),
    "shares": (
        "shares", "quantity", "qty", "units", "titulos", "titulos actuales", "cantidad",
        "cantidad de titulos", "no de titulos", "numero de titulos", "acciones",
        "no titulos", "num titulos",
    ),
    "unit_cost": (
        "avg cost", "avg_cost", "average cost", "average price", "avg price", "unit cost",
        "unit_cost", "cost per share", "cost share", "purchase price", "costo promedio",
        "costo promedio de adquisicion", "costo prom", "costo promedio unitario",
        "precio promedio", "precio promedio de compra", "costo unitario", "precio de compra",
        "costo de adquisicion unitario",
    ),
    "total_cost": (
        "total cost", "total_cost", "cost basis", "book cost", "costo total",
        "costo de adquisicion", "importe de adquisicion", "monto invertido", "inversion",
        "costo total de adquisicion",
    ),
    "market_price": (
        "market price", "last price", "close price", "current price", "precio de mercado",
        "precio actual", "precio de cierre", "ultimo precio", "precio mercado",
    ),
    "market_value": (
        "market value", "current value", "valor de mercado", "valuacion", "valor actual",
        "importe valuado", "valor mercado", "valuacion a mercado",
    ),
    "purchase_date": (
        "purchase date", "purchase_date", "trade date", "date", "fecha", "fecha de compra",
        "fecha de operacion", "fecha operacion", "fecha de concertacion", "fecha adquisicion",
        "fecha de adquisicion",
    ),
    "operation": (
        "operation", "activity", "transaction", "transaction type", "type", "action",
        "description", "operacion", "tipo de operacion", "tipo", "concepto", "descripcion",
        "movimiento",
    ),
    "price": ("price", "precio", "precio unitario", "unit price"),
    "amount": ("amount", "net amount", "importe", "monto", "importe neto", "neto"),
    "currency": ("currency", "moneda", "divisa"),
    "market": ("market", "mercado"),
    "broker": ("broker", "casa de bolsa", "intermediario"),
    "notes": ("notes", "notas", "comentarios"),
}

_SYNONYM_LOOKUP: Dict[str, str] = {}
for _field, _names in HEADER_SYNONYMS.items():
    for _n in _names:
        _SYNONYM_LOOKUP.setdefault(_n, _field)

# Rows that are cash or totals, not instruments.
_NON_INSTRUMENT = re.compile(
    r"^(total(es)?|subtotal|efectivo|cash|saldo|liquidez|smart cash|reporto|"
    r"fondos?|sociedades de inversion|grand total)\b",
    re.IGNORECASE,
)

_SPANISH_MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
    "jan": 1, "apr": 4, "aug": 8, "dec": 12,
}


class HoldingsError(ValueError):
    """The holdings input cannot be turned into positions."""


# ---------------------------------------------------------------------------
# Text helpers (shared with the PDF parser)
# ---------------------------------------------------------------------------

def normalize_header(text: object) -> str:
    """Lower-case, strip accents and punctuation: 'Títulos Actuales' -> 'titulos actuales'."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canonical_field(header: object) -> Optional[str]:
    """Map a raw column header to a canonical field name, or None."""
    h = normalize_header(header)
    if not h:
        return None
    if h in _SYNONYM_LOOKUP:
        return _SYNONYM_LOOKUP[h]
    # A header with a trailing currency or unit, e.g. "costo promedio mxn".
    trimmed = re.sub(r"\b(mxn|usd|mn|dls|pesos|dolares|\$)\b", "", h).strip()
    trimmed = re.sub(r"\s+", " ", trimmed)
    return _SYNONYM_LOOKUP.get(trimmed)


def parse_number(value: object) -> Optional[float]:
    """Parse '3,850.25', '$ 3,850.25 MXN', '(12.5)', '3.850,25' -> float. None if empty."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        f = float(value)
        return None if f != f else f
    s = str(value).strip()
    if not s or s in {"-", "--", "—", "n/a", "N/A", "nan", "NaN"}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"(?i)\b(mxn|usd|mn|dls|us)\b", "", s)
    s = re.sub(r"[^0-9,.\-]", "", s)
    if not s or s in {"-", ".", ","}:
        return None
    if s.startswith("-"):
        negative, s = True, s[1:]
    s = s.replace("-", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 3.850,25
            s = s.replace(".", "").replace(",", ".")
        else:                                      # 3,850.25
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if all(len(p) == 3 for p in parts[1:]) and parts[0] and parts[0] != "0":
            s = s.replace(",", "")                 # 1,234 / 1,234,567
        else:
            s = s.replace(",", ".")                # 12,5 / 0,123
    elif s.count(".") > 1:
        s = s.replace(".", "")                     # 1.234.567
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if negative else f


_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_DMY = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")
_DATE_DMONY = re.compile(r"^(\d{1,2})[/\-. ]([A-Za-z]{3,})\.?[/\-. ](\d{2,4})$")


def parse_date(value: object) -> Optional[date]:
    """ISO or day-first dates (see module docstring). None if empty; raises on garbage."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none", "-"}:
        return None
    s = re.sub(r"\s+", " ", s)

    m = _DATE_ISO.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return _mkdate(y, mo, d, s)
    m = _DATE_DMY.match(s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        return _mkdate(_year(y), mo, d, s)
    m = _DATE_DMONY.match(s)
    if m:
        d, mon, y = m.groups()
        mo = _SPANISH_MONTHS.get(normalize_header(mon)[:3])
        if mo is None:
            raise ValueError(f"unrecognised month in date {value!r}")
        return _mkdate(_year(int(y)), mo, int(d), s)
    raise ValueError(
        f"unrecognised date {value!r}; use YYYY-MM-DD or day-first DD/MM/YYYY"
    )


def _year(y: int) -> int:
    return 2000 + y if y < 100 else y


def _mkdate(y: int, mo: int, d: int, raw: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise ValueError(f"invalid date {raw!r}: {exc}") from None


# ---------------------------------------------------------------------------
# Ticker / market normalization
# ---------------------------------------------------------------------------

@dataclass
class ResolvedInstrument:
    ticker: str
    market: str
    currency: str
    analysis_symbol: str
    listing_currency: str
    in_selection_coverage: bool
    warnings: List[str] = field(default_factory=list)


def resolve_instrument(
    raw_ticker: str,
    *,
    market: Optional[str] = None,
    currency: Optional[str] = None,
    series: Optional[str] = None,
) -> ResolvedInstrument:
    """Turn a broker ticker plus optional market/currency into an analysable instrument.

    ``NVDA *`` (SIC) -> NVDA analysed on its US listing, carried in MXN.
    ``GFNORTE O`` (BMV) -> GFNORTEO.MX, priced and carried in MXN.
    ``BRK B`` / ``BRK.B`` / ``BRK-B`` (US) -> BRK.B, analysed as BRK-B on Yahoo.
    """
    warnings: List[str] = []
    raw = re.sub(r"\s+", " ", str(raw_ticker or "").strip().upper())
    if not raw:
        raise HoldingsError("empty ticker")
    tokens = raw.replace("*", " * ").split()
    root = tokens[0]
    suffix = (series or (tokens[1] if len(tokens) > 1 else "")).strip().upper()
    if suffix == "*":
        suffix = ""

    mkt = (market or "").strip().upper() or None
    ccy = (currency or "").strip().upper() or None
    if mkt is not None and mkt not in ("US", "SIC", "BMV"):
        raise HoldingsError(f"{raw}: market must be US, SIC or BMV, got {market!r}")
    if ccy is not None and ccy not in ("USD", "MXN"):
        raise HoldingsError(f"{raw}: currency must be USD or MXN, got {currency!r}")
    if root.endswith(".MX"):
        root, mkt = root[:-3], mkt or "BMV"

    if mkt is None:
        if ccy == "MXN":
            mkt = "SIC"
            warnings.append(
                "MARKET_ASSUMED_SIC: an MXN position was assumed to be a US stock bought "
                "through the SIC; set market=BMV for a Mexican listing"
            )
        else:
            mkt = "US"
            if ccy is None:
                warnings.append("CURRENCY_ASSUMED_USD: no currency given; assumed a USD position")
    if ccy is None:
        ccy = "USD" if mkt == "US" else "MXN"

    if mkt == "US" and ccy == "MXN":
        mkt = "SIC"
        warnings.append("MARKET_SET_TO_SIC: a US listing carried in MXN is an SIC position")
    if mkt == "BMV" and ccy == "USD":
        raise HoldingsError(f"{raw}: a BMV listing cannot be carried in USD")

    if mkt == "BMV":
        ticker = f"{root}{suffix}"
        return ResolvedInstrument(
            ticker=ticker, market="BMV", currency="MXN",
            analysis_symbol=f"{ticker}.MX", listing_currency="MXN",
            in_selection_coverage=False, warnings=warnings,
        )

    # US / SIC: a US listing. Accept BRK.B, BRK-B and "BRK B" for share classes.
    if "." in root or "-" in root:
        ticker = root.replace("-", ".")
    elif suffix in ("A", "B", "C"):
        ticker = f"{root}.{suffix}"
    else:
        ticker = root
        if suffix:
            warnings.append(f"SERIES_IGNORED: series {suffix!r} dropped for US listing {root}")
    return ResolvedInstrument(
        ticker=ticker, market=mkt, currency=ccy,
        analysis_symbol=to_provider_symbol(ticker, "yfinance"), listing_currency="USD",
        in_selection_coverage=True, warnings=warnings,
    )


def is_non_instrument(label: object) -> bool:
    return bool(_NON_INSTRUMENT.match(normalize_header(label)))


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

@dataclass
class LoadReport:
    source: str
    rows_read: int = 0
    lots: int = 0
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    column_mapping: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "rows_read": self.rows_read,
            "lots": self.lots,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "column_mapping": self.column_mapping,
        }


def map_columns(columns: Iterable[object]) -> Dict[str, str]:
    """canonical field -> original column (first match wins)."""
    mapping: Dict[str, str] = {}
    for col in columns:
        f = canonical_field(col)
        if f and f not in mapping:
            mapping[f] = str(col)
    return mapping


def load_holdings_csv(path: Union[str, Path]) -> Tuple[List[Lot], LoadReport]:
    """Read the holdings template (or any CSV whose headers the vocabulary knows)."""
    p = Path(path)
    if not p.exists():
        raise HoldingsError(f"holdings file not found: {p}")
    df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    report = LoadReport(source=str(p), rows_read=len(df))
    mapping = map_columns(df.columns)
    report.column_mapping = mapping

    missing = [f for f in ("ticker", "shares") if f not in mapping]
    if "unit_cost" not in mapping and "total_cost" not in mapping:
        missing.append("avg_cost (or total_cost)")
    if missing:
        raise HoldingsError(
            f"{p.name}: missing required column(s): {', '.join(missing)}. "
            f"Columns found: {list(df.columns)}. See configs/us_equities/holdings_template.csv"
        )

    lots: List[Lot] = []
    for i, row in df.iterrows():
        where = f"{p.name} row {i + 2}"           # +2: header line and 1-based rows
        lot = _row_to_lot({f: row[c] for f, c in mapping.items()}, where, report)
        if lot is not None:
            lots.append(lot)
    report.lots = len(lots)
    if not lots:
        raise HoldingsError(f"{p.name}: no usable holdings rows ({len(report.skipped)} skipped)")
    return lots, report


def _row_to_lot(values: Dict[str, object], where: str, report: LoadReport) -> Optional[Lot]:
    raw_ticker = str(values.get("ticker") or "").strip()
    if not raw_ticker or is_non_instrument(raw_ticker):
        if raw_ticker:
            report.skipped.append(f"{where}: {raw_ticker!r} is cash/total, not an instrument")
        return None
    try:
        inst = resolve_instrument(
            raw_ticker,
            market=_s(values.get("market")),
            currency=_s(values.get("currency")),
            series=_s(values.get("series")),
        )
    except HoldingsError as exc:
        report.skipped.append(f"{where}: {exc}")
        return None

    shares = parse_number(values.get("shares"))
    if shares is None or shares <= 0:
        report.skipped.append(f"{where}: {raw_ticker} has no positive share count")
        return None

    derived: List[str] = []
    unit_cost = parse_number(values.get("unit_cost"))
    if unit_cost is None:
        total = parse_number(values.get("total_cost"))
        if total is not None and total > 0:
            unit_cost = total / shares
            derived.append("unit_cost")
    if unit_cost is None or unit_cost <= 0:
        report.skipped.append(f"{where}: {raw_ticker} has no positive cost (never guessed)")
        return None

    purchase: Optional[date] = None
    raw_date = values.get("purchase_date")
    if _s(raw_date):
        try:
            purchase = parse_date(raw_date)
        except ValueError as exc:
            report.warnings.append(f"{where}: {exc}; purchase date left blank")
    if purchase is not None and purchase > date.today():
        report.warnings.append(f"{where}: purchase date {purchase} is in the future; left blank")
        purchase = None

    for w in inst.warnings:
        report.warnings.append(f"{where}: {w}")
    return Lot(
        ticker=inst.ticker, shares=float(shares), unit_cost=float(unit_cost),
        currency=inst.currency, market=inst.market, purchase_date=purchase,
        broker=_s(values.get("broker")), source=where, raw_ticker=raw_ticker,
        derived_fields=derived,
    )


def _s(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_lots(lots: List[Lot]) -> List[Holding]:
    """Group lots into holdings by (ticker, market, currency), preserving input order."""
    groups: "OrderedDict[Tuple[str, str, str], List[Lot]]" = OrderedDict()
    for lot in lots:
        groups.setdefault((lot.ticker, lot.market, lot.currency), []).append(lot)

    holdings: List[Holding] = []
    for (ticker, market, currency), group in groups.items():
        inst = resolve_instrument(ticker, market=market, currency=currency)
        h = Holding(
            ticker=ticker, market=market, currency=currency,
            analysis_symbol=inst.analysis_symbol, listing_currency=inst.listing_currency,
            lots=group, in_selection_coverage=inst.in_selection_coverage,
        )
        if not h.dates_complete:
            missing = sum(1 for l in group if l.purchase_date is None)
            h.warnings.append(
                f"PURCHASE_DATE_MISSING: {missing} of {len(group)} lot(s) have no purchase date; "
                "holding period and the stock/FX split are unavailable for them"
            )
        if not inst.in_selection_coverage:
            h.warnings.append(
                "OUTSIDE_SELECTION_COVERAGE: Mexican listings are not scored by the US-equity "
                "selection model; this position is analysed on price, trend and forecast only"
            )
        holdings.append(h)
    return holdings


def write_holdings_csv(lots: List[Lot], path: Union[str, Path]) -> Path:
    """Write lots in the template format (used to make PDF extraction auditable)."""
    rows = []
    for l in lots:
        rows.append({
            "ticker": l.raw_ticker or l.ticker,
            "shares": l.shares,
            "avg_cost": round(l.unit_cost, 6),
            "currency": l.currency,
            "market": l.market,
            "purchase_date": l.purchase_date.isoformat() if l.purchase_date else "",
            "broker": l.broker or "",
            "notes": f"from {l.source}" + (
                f"; derived: {', '.join(l.derived_fields)}" if l.derived_fields else ""
            ),
        })
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=TEMPLATE_COLUMNS).to_csv(out, index=False)
    return out
