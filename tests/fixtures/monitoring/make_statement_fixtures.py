"""Regenerate the synthetic statement PDFs used by test_monitoring_pdf.py.

These are SYNTHETIC: their layout imitates the vocabulary of a GBM "estado de
cuenta", a DriveWealth account statement and DriveWealth trade confirmations, but they are not copies of real
statements and contain no real account data. A real statement may lay things
out differently; the parser's report is built to make that visible.

    pip install fpdf2
    python tests/fixtures/monitoring/make_statement_fixtures.py
"""
from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

HERE = Path(__file__).resolve().parent


def _pdf() -> FPDF:
    pdf = FPDF(orientation="L", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", size=9)
    return pdf


def _line(pdf: FPDF, text: str, size: int = 9, bold: bool = False) -> None:
    pdf.set_font("Helvetica", "B" if bold else "", size)
    pdf.cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)


def _table(pdf: FPDF, rows, headings: bool = True) -> None:
    with pdf.table(first_row_as_headings=headings, line_height=6) as table:
        for r in rows:
            row = table.row()
            for cell in r:
                row.cell(str(cell))


def gbm_statement(path: Path) -> None:
    pdf = _pdf()
    _line(pdf, "GBM Grupo Bursatil Mexicano, S.A. de C.V., Casa de Bolsa", 11, bold=True)
    _line(pdf, "ESTADO DE CUENTA - Periodo del 01/09/2026 al 30/09/2026")
    _line(pdf, "Contrato: 0000000 (sintetico)")
    pdf.ln(8)
    _line(pdf, "ACCIONES SIC - MERCADO GLOBAL", bold=True)
    _table(pdf, [
        ["Emisora", "Serie", "Titulos", "Costo Promedio", "Precio de Mercado", "Valor de Mercado"],
        ["NVDA", "*", "10", "3,850.00", "4,131.25", "41,312.50"],
        ["AAPL", "*", "3", "3,400.00", "6,224.06", "18,672.18"],
        ["Efectivo", "", "", "", "", "1,250.00"],
    ])
    pdf.ln(14)
    _line(pdf, "ACCIONES NACIONALES (BMV)", bold=True)
    _table(pdf, [
        ["Emisora", "Serie", "Titulos", "Costo Promedio", "Precio de Mercado", "Valor de Mercado"],
        ["WALMEX", "*", "40", "62.15", "47.30", "1,892.00"],
        ["GFNORTE", "O", "25", "150.00", "171.20", "4,280.00"],
    ])
    pdf.add_page()
    _line(pdf, "MOVIMIENTOS DEL PERIODO", bold=True)
    _table(pdf, [
        ["Fecha", "Operacion", "Emisora", "Serie", "Titulos", "Precio", "Importe"],
        ["05/09/2026", "Compra", "AAPL", "*", "3", "3,380.00", "10,140.00"],
        ["12/09/2026", "Compra", "NVDA", "*", "4", "3,900.00", "15,600.00"],
        ["15/09/2026", "Deposito", "", "", "", "", "20,000.00"],
    ])
    pdf.output(str(path))


def gbm_multipage(path: Path) -> None:
    pdf = _pdf()
    _line(pdf, "GBM Casa de Bolsa - ESTADO DE CUENTA", 11, bold=True)
    pdf.ln(110)
    _line(pdf, "ACCIONES SIC", bold=True)
    _table(pdf, [
        ["Emisora", "Serie", "Titulos", "Costo Promedio", "Valor de Mercado"],
        ["MSFT", "*", "2", "7,400.00", "15,100.00"],
        ["AMZN", "*", "5", "3,600.00", "19,000.00"],
    ])
    pdf.add_page()
    _table(pdf, [
        ["GOOGL", "*", "4", "3,100.00", "13,500.00"],
        ["META", "*", "1", "11,000.00", "12,400.00"],
    ], headings=False)
    pdf.output(str(path))


def drivewealth_statement(path: Path) -> None:
    pdf = _pdf()
    _line(pdf, "DriveWealth, LLC - Account Statement (synthetic)", 11, bold=True)
    _line(pdf, "Statement period: September 1, 2026 - September 30, 2026")
    pdf.ln(8)
    _line(pdf, "Holdings", bold=True)
    _table(pdf, [
        ["Symbol", "Quantity", "Cost Basis", "Market Price", "Market Value"],
        ["AAPL", "2.5", "450.50", "340.97", "852.43"],
        ["MSFT", "1.2", "492.00", "415.30", "498.36"],
    ])
    pdf.ln(12)
    _line(pdf, "Account Activity", bold=True)
    _table(pdf, [
        ["Date", "Activity", "Symbol", "Quantity", "Price", "Amount"],
        ["2026-09-03", "Buy", "MSFT", "1.2", "410.00", "492.00"],
        ["2026-09-10", "Dividend", "AAPL", "", "", "0.65"],
    ])
    pdf.output(str(path))


CONFIRM_HEAD = [
    "Transaction GBM",
    "Insurgentes Sur 0000 - Floor 0 (synthetic)",
    "Confirmation",
    "Account Number:GBMP-001-SYN00042 Account Name: SYNTHETIC TEST ACCOUNT",
]
CONFIRM_COLUMNS = ("Symbol Security A/CType Action Execution Time Quantity Price Trade Date "
                   "Settle Date Capacity")


def _money(v: float) -> str:
    return f"(${-v:,.2f})" if v < 0 else f"${v:,.2f}"


def _trade(pdf: FPDF, line: str, principal: float, commission: float, net: float) -> None:
    pdf.set_font("Helvetica", size=8)
    for text in (line, f"Principal Amount {_money(principal)}", "Interest", f"Commission {_money(commission)}",
                 "Transaction Fee $0.00", "Other Fees / Credits $0.00", f"Net Amount {_money(net)}"):
        pdf.cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")


def _confirm_page(pdf: FPDF, date_line: str = "") -> None:
    pdf.add_page()
    for text in CONFIRM_HEAD:
        _line(pdf, text)
    if date_line:
        _line(pdf, f"Confirmation Date : {date_line}")
    _line(pdf, CONFIRM_COLUMNS, 8)


def drivewealth_confirmation_buys(path: Path) -> None:
    """Three orders on one day, one of them filled as whole + fractional executions, across two pages."""
    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    _confirm_page(pdf, "3/2/2026")
    _trade(pdf, "NVDA NVIDIA CORPORATION COM M Buy 9:31:05 AM 0.5 180.00 3/2/2026 3/3/2026 Principal",
           90.00, 0.22, 90.22)
    pdf.add_page()
    _line(pdf, "Transaction Terms")
    _line(pdf, "1. This trade confirmation is synthetic test data.")
    _confirm_page(pdf)
    _trade(pdf, "AAPL APPLE INC COM M Buy 10:02:11 AM 2 250.00 3/2/2026 3/3/2026 Agency", 500.00, 0.00, 500.00)
    _trade(pdf, "AAPL APPLE INC COM M Buy 10:02:12 AM 0.4 250.00 3/2/2026 3/3/2026 Principal", 100.00, 0.24, 100.24)
    _trade(pdf, "BRK.B BERKSHIRE HATHAWAY INC DEL CL B NEW M Buy 10:05:40 AM 0.1 500.00 3/2/2026 3/3/2026 "
                "Riskless Principal", 50.00, 0.12, 50.12)
    pdf.output(str(path))


def drivewealth_confirmation_sells(path: Path) -> None:
    """A partial sale, a full sale at a loss, and a sale with no known purchase."""
    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    _confirm_page(pdf, "4/15/2026")
    _trade(pdf, "AAPL APPLE INC COM M Sell 3:58:00 PM -1 275.00 4/15/2026 4/16/2026 Agency", -275.00, 0.00, -275.00)
    _trade(pdf, "NVDA NVIDIA CORPORATION COM M Sell 11:15:30 AM -0.5 170.00 4/15/2026 4/16/2026 Principal",
           -85.00, 0.20, -84.80)
    _trade(pdf, "TSLA TESLA INC COM M Sell 12:00:00 PM -0.25 400.00 4/15/2026 4/16/2026 Principal",
           -100.00, 0.24, -99.76)
    pdf.output(str(path))


def no_holdings(path: Path) -> None:
    pdf = _pdf()
    _line(pdf, "GBM Casa de Bolsa - ESTADO DE CUENTA", 11, bold=True)
    _line(pdf, "Este periodo no hubo posiciones en acciones.")
    pdf.output(str(path))


if __name__ == "__main__":
    gbm_statement(HERE / "gbm_statement.pdf")
    gbm_multipage(HERE / "gbm_multipage.pdf")
    drivewealth_statement(HERE / "drivewealth_statement.pdf")
    no_holdings(HERE / "no_holdings.pdf")
    drivewealth_confirmation_buys(HERE / "confirmation_buys.pdf")
    drivewealth_confirmation_sells(HERE / "confirmation_sells.pdf")
    print("fixtures written to", HERE)
