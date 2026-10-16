"""Read trade confirmations (PDF) into executions.

Supported today: the confirmations GBM's US-dollar account sends for every
trading day, issued by its clearing firm DriveWealth ("Transaction GBM ...
Confirmation ... Account Number: GBMP-..."). Each trade is one line,

    NVDA NVIDIA CORPORATION COM M Buy 10:16:52 AM 0.11815254 122.4688 2/5/2025 2/6/2025 Principal

followed by its own amounts block (Principal Amount, Commission, Transaction
Fee, Other Fees / Credits, Net Amount); sells print a negative quantity and
amounts in parentheses. A file can hold many trades across several pages.

Nothing is guessed. Every trade line must match the layout above, its
amounts are cross-checked (quantity x price against the principal; principal
plus or minus costs against the net amount), and anything that looks like a
trade but does not parse is reported, so a file is either read completely or
says what it could not read. The account holder's name is not kept and the
account number is masked to its last four characters.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, List, Optional, Union

from .holdings import HoldingsError
from .ledger import BUY, SELL, Execution

TRADE_LINE = re.compile(
    r"^(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+(?P<desc>.+?)\s+(?P<actype>[A-Z])\s+(?P<action>Buy|Sell)\s+"
    r"(?P<time>\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s+(?P<qty>-?[\d,]*\.?\d+)\s+(?P<price>[\d,]*\.?\d+)\s+"
    r"(?P<trade>\d{1,2}/\d{1,2}/\d{4})\s+(?P<settle>\d{1,2}/\d{1,2}/\d{4})\s*(?P<capacity>.*)$"
)
LOOKS_LIKE_TRADE = re.compile(r"\b(Buy|Sell)\b.*\d{1,2}:\d{2}:\d{2}\s*[AP]M")
AMOUNT = re.compile(r"^(?P<label>Principal Amount|Commission|Transaction Fee|Other Fees / Credits|Net Amount)"
                    r"\s+(?P<value>\(?-?\$?[\d,]*\.?\d+\)?)\s*$")
ACCOUNT = re.compile(r"Account Number\s*:\s*(?P<acct>[A-Z0-9\-]+)")


class ConfirmationParseError(HoldingsError):
    """The file is not a trade confirmation this parser knows."""


@dataclass
class ConfirmationReport:
    source: str
    pages: int = 0
    trades: int = 0
    account: Optional[str] = None
    confirmation_date: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    unreadable_lines: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.trades > 0 and not self.unreadable_lines


def parse_confirmation_pdf(source: Union[str, Path, BinaryIO], *, name: Optional[str] = None):
    """``(executions, report)`` for one confirmation PDF (path or file object)."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise ConfirmationParseError(
            "reading PDF confirmations needs pdfplumber: pip install -e \".[pdf]\""
        ) from exc
    label = name or (Path(source).name if isinstance(source, (str, Path)) else "upload.pdf")
    try:
        with pdfplumber.open(source) as pdf:
            pages = [(i + 1, page.extract_text() or "") for i, page in enumerate(pdf.pages)]
    except Exception as exc:  # noqa: BLE001 - pdfminer raises many types on a bad file
        raise ConfirmationParseError(f"{label}: cannot read the PDF ({type(exc).__name__})") from None
    return parse_confirmation_text(pages, source=label)


def parse_confirmation_text(pages, *, source: str = "confirmation.pdf"):
    """Parse already-extracted page text: a list of ``(page_number, text)``."""
    report = ConfirmationReport(source=source, pages=len(pages))
    full = "\n".join(t for _, t in pages)
    if "Confirmation" not in full or "Symbol" not in full or "Net Amount" not in full:
        raise ConfirmationParseError(
            f"{source}: this does not look like a trade confirmation (no trade table found). "
            "Monthly statements go to broker option A on the Monitor page."
        )
    m = ACCOUNT.search(full)
    if m:
        report.account = "…" + m.group("acct")[-4:]
    m = re.search(r"Confirmation Date\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", full)
    if m:
        report.confirmation_date = _date(m.group(1)).isoformat()

    executions: List[Execution] = []
    current: Optional[dict] = None

    def finish():
        if current is not None:
            executions.append(_execution(current, report))

    for page_no, text in pages:
        for raw in text.splitlines():
            line = raw.strip()
            t = TRADE_LINE.match(line)
            if t:
                finish()
                current = {"m": t, "amounts": {}, "source": f"{source} p{page_no}"}
                continue
            a = AMOUNT.match(line)
            if a and current is not None:
                current["amounts"].setdefault(a.group("label"), _money(a.group("value")))
                continue
            if LOOKS_LIKE_TRADE.search(line) and not line.startswith("Symbol"):
                report.unreadable_lines.append(f"p{page_no}: {line[:120]}")
    finish()
    report.trades = len(executions)
    if not executions and not report.unreadable_lines:
        raise ConfirmationParseError(f"{source}: a confirmation, but it lists no trades")
    return executions, report


def _execution(cur: dict, report: ConfirmationReport) -> Execution:
    m, amounts = cur["m"], cur["amounts"]
    qty = abs(_num(m.group("qty")))
    price = _num(m.group("price"))
    side = BUY if m.group("action") == "Buy" else SELL
    principal = abs(amounts.get("Principal Amount", qty * price))
    commission = abs(amounts.get("Commission", 0.0))
    fees = abs(amounts.get("Transaction Fee", 0.0)) + amounts.get("Other Fees / Credits", 0.0)
    net = abs(amounts["Net Amount"]) if "Net Amount" in amounts else (
        principal + commission + fees if side == BUY else principal - commission - fees)
    where = f"{m.group('symbol')} {m.group('action')} {m.group('trade')}"
    if "Net Amount" not in amounts:
        report.warnings.append(f"{where}: no Net Amount printed; computed from principal and costs")
    if abs(qty * price - principal) > max(0.02, principal * 0.002):
        report.warnings.append(f"{where}: quantity x price = {qty * price:.2f} but principal is {principal:.2f}")
    expected = principal + commission + fees if side == BUY else principal - commission - fees
    if abs(expected - net) > 0.015:
        report.warnings.append(f"{where}: net amount {net:.2f} does not equal principal and costs ({expected:.2f})")
    return Execution(
        symbol=m.group("symbol"), side=side, quantity=qty, price=price, trade_date=_date(m.group("trade")),
        exec_time=re.sub(r"\s+", " ", m.group("time")).upper(), settle_date=_date(m.group("settle")),
        description=m.group("desc").strip(), principal=principal, commission=commission, fees=fees,
        net_amount=net, currency="USD", capacity=m.group("capacity").strip() or None, account=report.account,
        source=cur["source"],
    )


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _money(text: str) -> float:
    t = text.strip()
    negative = t.startswith("(") or t.startswith("-")
    value = float(t.strip("()").replace("$", "").replace(",", "").lstrip("-") or 0)
    return -value if negative else value


def _date(text: str) -> date:
    return datetime.strptime(text, "%m/%d/%Y").date()
