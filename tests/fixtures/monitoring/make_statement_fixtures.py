"""Regenerate the synthetic statement PDFs used by test_monitoring_pdf.py.

These are SYNTHETIC: their layout imitates the vocabulary of a GBM "estado de
cuenta" and a DriveWealth account statement, but they are not copies of real
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
    print("fixtures written to", HERE)
