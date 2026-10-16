"""Trade confirmations (PDF) -> executions -> orders -> FIFO-matched operations.

The PDFs are SYNTHETIC (tests/fixtures/monitoring/confirmation_*.pdf, built
by make_statement_fixtures.py). Their line layout copies the DriveWealth
confirmations GBM's US-dollar account issues -- calibrated on 16 real
confirmations (53 trades) that are not part of the repository.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from asset_selection.monitoring.confirmations import ConfirmationParseError, parse_confirmation_text
from asset_selection.monitoring.holdings import load_holdings_csv
from asset_selection.monitoring.ledger import (
    BUY,
    SELL,
    Execution,
    consolidate,
    csv_safe,
    group_orders,
    holdings_csv_text,
    merge_new,
    read_executions,
    security_name,
    write_executions,
)

FIX = Path("tests/fixtures/monitoring")
HEAD = ("Transaction GBM\nConfirmation\nAccount Number:GBMP-001-ABCD000123 Account Name: SOMEONE\n"
        "Confirmation Date : 5/22/2026\n"
        "Symbol Security A/CType Action Execution Time Quantity Price Trade Date Settle Date Capacity\n")


def _amounts(principal, commission, net):
    return (f"Principal Amount {principal}\nInterest\nCommission {commission}\nTransaction Fee $0.00\n"
            f"Other Fees / Credits $0.00\nNet Amount {net}\n")


def _ex(symbol, side, qty, price, day, t="10:00:00 AM", commission=0.0):
    principal = round(qty * price, 2)
    net = principal + commission if side == BUY else principal - commission
    return Execution(symbol=symbol, side=side, quantity=qty, price=price, trade_date=date.fromisoformat(day),
                     exec_time=t, principal=principal, commission=commission, net_amount=net,
                     description=f"{symbol} INC COM")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_a_sell_line_with_parenthesised_amounts():
    text = HEAD + ("ASTS AST SPACEMOBILE INC COM CL A M Sell 3:33:44 PM -0.90083115 104.4212 5/22/2026 "
                   "5/26/2026 Principal\n" + _amounts("($94.07)", "$0.23", "($93.84)"))
    (e,), report = parse_confirmation_text([(1, text)], source="c.pdf")
    assert (e.symbol, e.side, e.quantity, e.price) == ("ASTS", SELL, 0.90083115, 104.4212)
    assert e.description == "AST SPACEMOBILE INC COM CL A"            # "CL A" stays in the name, "M" is the type
    assert (e.principal, e.commission, e.net_amount) == (94.07, 0.23, 93.84)
    assert e.trade_date == date(2026, 5, 22) and e.settle_date == date(2026, 5, 26)
    assert e.exec_time == "3:33:44 PM" and e.capacity == "Principal"
    assert report.account == "…0123" and report.ok and report.warnings == []


def test_each_trade_keeps_its_own_amounts_across_pages():
    page1 = HEAD + ("RGTI RIGETTI COMPUTING INC COMMON STOCK M Buy 11:33:50 AM 0.27874045 53.67 10/14/2025 "
                    "10/15/2025 Riskless Principal\n" + _amounts("$14.96", "$0.03", "$14.99"))
    page3 = HEAD + ("PATH UIPATH INC CL A M Buy 11:36:16 AM 1 16.5588 10/14/2025 10/15/2025 Agency\n"
                    + _amounts("$16.56", "$0.00", "$16.56"))
    ex, report = parse_confirmation_text([(1, page1), (2, "Transaction Terms\n1. Terms"), (3, page3)])
    assert [(e.symbol, e.net_amount, e.source[-2:]) for e in ex] == [("RGTI", 14.99, "p1"), ("PATH", 16.56, "p3")]
    assert ex[0].capacity == "Riskless Principal" and report.trades == 2


def test_inconsistent_amounts_are_reported_not_hidden():
    text = HEAD + ("XOM EXXON MOBIL CORP COM M Buy 9:50:52 AM 0.866713 115.09 6/23/2025 6/24/2025 Principal\n"
                   + _amounts("$99.75", "$0.24", "$120.00"))
    (e,), report = parse_confirmation_text([(1, text)])
    assert e.net_amount == 120.0
    assert any("net amount 120.00" in w for w in report.warnings)


def test_a_line_that_looks_like_a_trade_but_does_not_parse_is_listed():
    text = HEAD + "NVDA ??? Buy 10:16:52 AM garbled\n" + _amounts("$14.47", "$0.00", "$14.47")
    with pytest.raises(ConfirmationParseError):
        parse_confirmation_text([(1, "no trade table here")])
    ex, report = parse_confirmation_text([(1, text)])
    assert ex == [] and report.unreadable_lines and not report.ok


def test_synthetic_pdfs_parse_completely():
    pytest.importorskip("pdfplumber")
    from asset_selection.monitoring.confirmations import parse_confirmation_pdf

    buys, r1 = parse_confirmation_pdf(FIX / "confirmation_buys.pdf")
    sells, r2 = parse_confirmation_pdf(FIX / "confirmation_sells.pdf")
    assert (r1.trades, r2.trades, r1.pages) == (4, 3, 3)
    assert r1.ok and r2.ok and r1.warnings == r2.warnings == []
    assert [e.symbol for e in buys] == ["NVDA", "AAPL", "AAPL", "BRK.B"]
    assert [e.side for e in sells] == [SELL, SELL, SELL]
    with pytest.raises(ConfirmationParseError, match="does not look like a trade confirmation"):
        parse_confirmation_pdf(FIX / "drivewealth_statement.pdf")     # a monthly statement, not a confirmation


# ---------------------------------------------------------------------------
# Orders and FIFO
# ---------------------------------------------------------------------------

def test_whole_and_fractional_executions_become_one_order():
    orders = group_orders([
        _ex("BTBT", BUY, 9, 2.0092, "2026-05-13", "11:27:20 AM"),
        _ex("BTBT", BUY, 0.9293251, 2.0092, "2026-05-13", "11:27:21 AM"),
        _ex("BTBT", BUY, 1, 2.0092, "2026-05-13", "3:00:00 PM"),        # hours later: a second order
    ])
    assert [round(o.quantity, 7) for o in orders] == [9.9293251, 1.0]
    assert orders[0].executions == 2 and orders[0].id == "BTBT-2026-05-13-B1"


def test_fifo_partial_sale_full_sale_residue_and_unknown_purchase():
    view = consolidate([
        _ex("AAPL", BUY, 2, 100.0, "2026-01-05", commission=0.5),
        _ex("AAPL", BUY, 1, 120.0, "2026-02-05"),
        _ex("AAPL", SELL, 2.5, 130.0, "2026-03-05", commission=0.25),
        _ex("LMT", BUY, 0.10491684, 475.33, "2025-06-23"),
        _ex("LMT", SELL, 0.10430894, 508.68, "2025-10-06"),
        _ex("TSLA", SELL, 1, 400.0, "2026-04-01"),
    ])
    aapl = [o for o in view.operations if o.symbol == "AAPL"]
    assert [(o.status, o.shares, o.buy_price) for o in aapl] == [
        ("CLOSED", 2, 100.0), ("CLOSED", 0.5, 120.0), ("OPEN", 0.5, 120.0)]
    first = aapl[0]
    assert first.cost == pytest.approx(200.5)                          # commission included
    assert first.proceeds == pytest.approx(2 * (325.0 - 0.25) / 2.5)   # sale commission split pro rata
    assert first.realized_pl == pytest.approx(first.proceeds - 200.5)
    assert first.holding_days == 59
    assert "LMT" in view.residues and not any(o.symbol == "LMT" and o.status == "OPEN" for o in view.operations)
    unmatched = [o for o in view.operations if o.status == "UNMATCHED_SELL"]
    assert [o.symbol for o in unmatched] == ["TSLA"] and "import" in unmatched[0].note
    assert any("TSLA" in w for w in view.warnings) and any("LMT" in w for w in view.warnings)
    assert view.open_symbols == ["AAPL"]


def test_same_day_round_trip_buys_before_it_sells():
    view = consolidate([_ex("X", SELL, 1, 11.0, "2026-01-05", "3:00:00 PM"),
                        _ex("X", BUY, 1, 10.0, "2026-01-05", "10:00:00 AM")])
    assert [o.status for o in view.operations] == ["CLOSED"]


# ---------------------------------------------------------------------------
# Ledger file and holdings
# ---------------------------------------------------------------------------

def test_reimporting_the_same_file_adds_nothing(tmp_path):
    batch = [_ex("NVDA", BUY, 0.5, 180.0, "2026-03-02"), _ex("NVDA", BUY, 0.5, 180.0, "2026-03-02")]
    added, dupes = merge_new([], batch)
    assert len(added) == 2 and dupes == 0                              # identical fills in one file both count
    added2, dupes2 = merge_new(added, batch)
    assert added2 == [] and dupes2 == 2
    path = tmp_path / "executions.csv"
    write_executions(path, added)
    assert read_executions(path) == added


def test_text_cells_cannot_become_spreadsheet_formulas(tmp_path):
    e = _ex("ABC", BUY, 1, 1.0, "2026-01-02")
    evil = Execution(**{**e.__dict__, "description": "=HYPERLINK(\"x\")"})
    path = tmp_path / "x.csv"
    write_executions(path, [evil])
    assert "'=HYPERLINK" in path.read_text() and read_executions(path)[0].description == evil.description
    assert csv_safe("+1") == "'+1" and csv_safe(-1.5) == -1.5


def test_open_lots_become_a_holdings_file_the_monitor_reads(tmp_path):
    view = consolidate([_ex("NVDA", BUY, 0.5, 180.0, "2026-03-02", commission=0.22),
                        _ex("BRK.B", BUY, 0.1, 500.0, "2026-03-02")])
    path = tmp_path / "holdings.csv"
    path.write_text(holdings_csv_text(view))
    lots, _ = load_holdings_csv(path)
    by = {lot.ticker: lot for lot in lots}
    assert set(by) == {"NVDA", "BRK.B"}
    assert by["NVDA"].unit_cost == pytest.approx(180.44) and by["NVDA"].currency == "USD"
    assert by["NVDA"].purchase_date == date(2026, 3, 2)


@pytest.mark.parametrize("raw, name", [
    ("NVIDIA CORPORATION COM", "Nvidia Corporation"),
    ("AST SPACEMOBILE INC COM CL A", "AST Spacemobile Inc"),
    ("ALPHABET INC CAP STK CL A", "Alphabet Inc"),
    ("ISHARES BITCOIN TRUST ETF SHS BEN INT", "Ishares Bitcoin Trust"),
    ("SUPER MICRO COMPUTER INC COM NEW", "Super Micro Computer Inc"),
])
def test_security_names_lose_the_share_class_jargon(raw, name):
    assert security_name(raw) == name
