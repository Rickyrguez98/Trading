"""Read-only holdings from Interactive Brokers' Flex Web Service (broker option C).

Flex Web Service v3 is IBKR's official read-only reporting API: a token and a
saved Flex Query ID (both created in Client Portal) return a statement. It
cannot place orders. The flow is two calls:

1. ``SendRequest?t=<token>&q=<query id>&v=3`` -> ``<FlexStatementResponse>``
   with ``Status`` ``Success`` and a ``ReferenceCode`` (plus the ``Url`` to
   poll), or ``Fail``/``Warn`` with ``ErrorCode``/``ErrorMessage``.
2. ``GetStatement?t=<token>&q=<reference code>&v=3`` -> the
   ``<FlexQueryResponse>`` XML, or a ``FlexStatementResponse`` saying the
   statement is still being generated (error 1019) -- poll again.

Credential handling, because the token reads your account:

* The token and query ID come **only from environment variables**
  (``IBKR_FLEX_TOKEN``, ``IBKR_FLEX_QUERY_ID`` by default). Nothing stores
  them, and the raw statement (which contains the account ID) is never
  written to disk -- only the extracted positions are.
* IBKR accepts the token only as the ``t`` query parameter, so request URLs
  are never logged and every error message is scrubbed of the token before
  it is raised.
* The token is only ever sent to ``https://*.interactivebrokers.com``; a
  polling URL pointing anywhere else is refused.

Field names follow the Flex XML schema (``OpenPosition`` attributes:
``symbol``, ``assetCategory``, ``currency``, ``listingExchange``,
``position``, ``costBasisPrice``, ``costBasisMoney``, ``levelOfDetail``,
``openDateTime``, ``side``). This connector is tested against synthetic
responses built on that schema; it has not been run against a live account.
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .holdings import HoldingsError, resolve_instrument
from .models import Lot

logger = logging.getLogger(__name__)

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
_ALLOWED_HOST = re.compile(r"(^|\.)interactivebrokers\.com$")
_HEADERS = {"User-Agent": "Trading-Research/flex-v3"}   # IBKR rejects requests without one

# Error codes that mean "not ready / busy -- ask again shortly".
RETRY_CODES = {"1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}
THROTTLED_CODES = {"1018"}
ERROR_HINTS = {
    "1011": "the Flex service account is inactive",
    "1012": "the token has expired -- generate a new one in Client Portal under Performance & Reports > Flex Queries > Flex Web Service Configuration",
    "1013": "IP restriction on the token",
    "1014": "the query ID is invalid",
    "1015": "the token is invalid",
    "1016": "the account is invalid",
    "1020": "the request could not be validated",
}

# Exchanges that mean a Mexican listing.
_MEXICAN_EXCHANGES = {"MEXI", "BMV", "MEXDER"}


class IbkrFlexError(HoldingsError):
    """The Flex request failed; the message never contains the token."""


@dataclass
class FlexReport:
    source: str = "ibkr_flex"
    positions_seen: int = 0
    lots: int = 0
    dates_attached: int = 0
    skipped: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    report_date: Optional[str] = None
    review_csv: Optional[str] = None
    confidence: str = "high"

    def to_dict(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "statement_type": "ibkr_flex",
            "holdings_rows": self.positions_seen,
            "lots": self.lots,
            "dates_attached": self.dates_attached,
            "buy_rows": 0,
            "skipped": self.skipped,
            "warnings": self.warnings,
            "report_date": self.report_date,
            "review_csv": self.review_csv,
            "confidence": self.confidence,
        }


@dataclass
class FlexCredentials:
    token: str
    query_id: str

    @classmethod
    def from_env(cls, token_env: str = "IBKR_FLEX_TOKEN",
                 query_id_env: str = "IBKR_FLEX_QUERY_ID") -> "FlexCredentials":
        token = (os.environ.get(token_env) or "").strip()
        query = (os.environ.get(query_id_env) or "").strip()
        missing = [n for n, v in ((token_env, token), (query_id_env, query)) if not v]
        if missing:
            raise IbkrFlexError(
                f"broker option C needs {' and '.join(missing)} in the environment "
                "(see .env.example and docs/us_equities/broker-integration-options.md)"
            )
        return cls(token=token, query_id=query)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def fetch_statement_xml(
    creds: FlexCredentials,
    *,
    poll_seconds: float = 5.0,
    max_wait_seconds: float = 120.0,
    http_get: Optional[Callable] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Run SendRequest + GetStatement and return the FlexQueryResponse XML bytes."""
    if http_get is None:
        import requests

        http_get = requests.get
    redact = _redactor(creds)

    def call(url: str, q: str) -> bytes:
        _require_ibkr_host(url)
        try:
            resp = http_get(url, params={"t": creds.token, "q": q, "v": "3"},
                            headers=_HEADERS, timeout=30)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - re-raised scrubbed of the token
            raise IbkrFlexError(f"IBKR Flex request failed: {redact(f'{type(exc).__name__}: {exc}')}") from None
        return resp.content

    body = call(SEND_REQUEST_URL, creds.query_id)
    status, data = _parse_status(body)
    if status != "Success":
        raise IbkrFlexError(_describe_error(data, redact))
    reference = (data.get("ReferenceCode") or "").strip()
    if not reference:
        raise IbkrFlexError("IBKR Flex SendRequest succeeded without a reference code")
    poll_url = (data.get("Url") or "").strip() or GET_STATEMENT_URL

    waited = 0.0
    while True:
        body = call(poll_url, reference)
        if b"FlexQueryResponse" in body[:2000]:
            return body
        status, data = _parse_status(body)
        code = (data.get("ErrorCode") or "").strip()
        if code in RETRY_CODES or code in THROTTLED_CODES:
            delay = poll_seconds * (2 if code in THROTTLED_CODES else 1)
            if waited + delay > max_wait_seconds:
                raise IbkrFlexError(f"IBKR Flex statement not ready after {waited:.0f}s "
                                    f"(last code {code}); try again in a few minutes")
            sleep(delay)
            waited += delay
            continue
        raise IbkrFlexError(_describe_error(data, redact))


def _require_ibkr_host(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not _ALLOWED_HOST.search(host):
        raise IbkrFlexError(f"refusing to send the Flex token to a non-IBKR address ({parsed.scheme}://{host})")


def _redactor(creds: FlexCredentials) -> Callable[[str], str]:
    secrets = [s for s in (creds.token,) if s]

    def redact(text: str) -> str:
        out = text
        for s in secrets:
            out = out.replace(s, "***")
        return re.sub(r"([?&]t=)[^&\s'\"]+", r"\1***", out)
    return redact


def _parse_status(body: bytes) -> Tuple[str, Dict[str, str]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise IbkrFlexError(f"IBKR Flex returned something that is not XML ({exc})") from None
    if root.tag != "FlexStatementResponse":
        raise IbkrFlexError(f"unexpected IBKR Flex response <{root.tag}>")
    data = {child.tag: (child.text or "") for child in root}
    return (data.get("Status") or "").strip(), data


def _describe_error(data: Dict[str, str], redact: Callable[[str], str]) -> str:
    code = (data.get("ErrorCode") or "").strip()
    msg = redact((data.get("ErrorMessage") or "").strip())
    hint = ERROR_HINTS.get(code)
    return f"IBKR Flex error {code or '?'}: {msg}" + (f" ({hint})" if hint else "")


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_open_positions(xml_bytes: bytes) -> Tuple[List[Lot], FlexReport]:
    """FlexQueryResponse XML -> lots (stocks only, long only)."""
    report = FlexReport()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise IbkrFlexError(f"could not parse the Flex statement ({exc})") from None
    rows = root.findall(".//OpenPositions/OpenPosition")
    if not rows:
        raise IbkrFlexError(
            "the Flex statement has no Open Positions section -- add 'Open Positions' to the "
            "Activity Flex Query (Client Portal > Performance & Reports > Flex Queries)"
        )
    report.positions_seen = len(rows)

    # A query may return both SUMMARY and LOT rows; lots carry the dates, and
    # counting both would double every position.
    lot_symbols = {r.get("symbol") for r in rows if (r.get("levelOfDetail") or "").upper() == "LOT"}
    lots: List[Lot] = []
    for i, r in enumerate(rows, start=1):
        a = r.attrib
        where = f"ibkr_flex position {i}"
        symbol = (a.get("symbol") or "").strip()
        level = (a.get("levelOfDetail") or "SUMMARY").upper()
        if level != "LOT" and symbol in lot_symbols:
            continue
        report.report_date = report.report_date or a.get("reportDate")
        cat = (a.get("assetCategory") or "").upper()
        if cat != "STK":
            report.skipped.append(f"{where}: {symbol or '?'} is {cat or 'unknown'}, not a stock or ETF")
            continue
        qty = _num(a.get("position"))
        if qty is None or qty == 0:
            continue
        if qty < 0 or (a.get("side") or "").lower() == "short":
            report.skipped.append(f"{where}: {symbol} is a short position; the monitor handles long holdings only")
            continue
        currency = (a.get("currency") or "").upper()
        exchange = (a.get("listingExchange") or "").upper()
        if currency == "MXN" or exchange in _MEXICAN_EXCHANGES:
            market = "BMV"
        elif currency == "USD":
            market = "US"
        else:
            report.skipped.append(f"{where}: {symbol} trades in {currency or '?'}; only USD and MXN are supported")
            continue
        unit = _num(a.get("costBasisPrice"))
        derived: List[str] = []
        if unit is None or unit <= 0:
            money = _num(a.get("costBasisMoney"))
            if money is not None and money > 0:
                unit, derived = money / qty, ["unit_cost"]
        if unit is None or unit <= 0:
            report.skipped.append(f"{where}: {symbol} has no cost basis (never guessed)")
            continue
        try:
            inst = resolve_instrument(symbol, market=market, currency="MXN" if market == "BMV" else "USD")
        except HoldingsError as exc:
            report.skipped.append(f"{where}: {exc}")
            continue
        opened = None
        if level == "LOT":
            opened = _flex_date(a.get("openDateTime"))
            if opened is None and a.get("openDateTime"):
                report.warnings.append(
                    f"{where}: {symbol} open date {a.get('openDateTime')!r} is not yyyyMMdd or "
                    "yyyy-MM-dd; set the query's date format to one of those"
                )
            elif opened is not None:
                report.dates_attached += 1
        lots.append(Lot(
            ticker=inst.ticker, shares=float(qty), unit_cost=float(unit), currency=inst.currency,
            market=inst.market, purchase_date=opened, broker="IBKR", source=where,
            raw_ticker=symbol, derived_fields=derived,
        ))
    if not any((r.get("levelOfDetail") or "").upper() == "LOT" for r in rows):
        report.warnings.append(
            "the Flex query reports positions at SUMMARY level, so purchase dates are unknown; "
            "set Open Positions to 'Lot' level of detail to get them"
        )
    report.lots = len(lots)
    if not lots:
        raise IbkrFlexError(f"no long stock positions in the Flex statement ({len(report.skipped)} skipped)")
    if report.skipped or any("unit_cost" in lot.derived_fields for lot in lots):
        report.confidence = "medium"
    return lots, report


def load_ibkr_lots(
    *,
    token_env: str = "IBKR_FLEX_TOKEN",
    query_id_env: str = "IBKR_FLEX_QUERY_ID",
    poll_seconds: float = 5.0,
    max_wait_seconds: float = 120.0,
    http_get: Optional[Callable] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[List[Lot], FlexReport]:
    creds = FlexCredentials.from_env(token_env, query_id_env)
    xml_bytes = fetch_statement_xml(creds, poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds,
                                    http_get=http_get, sleep=sleep)
    return parse_open_positions(xml_bytes)


def _num(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _flex_date(v: Optional[str]) -> Optional[date]:
    """IBKR date/time ('20250314;093000', '2025-03-14;09:30:00', '20250314') -> date."""
    if not v:
        return None
    head = re.split(r"[;,\sT]", str(v).strip())[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None
