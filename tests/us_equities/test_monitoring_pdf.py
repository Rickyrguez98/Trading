"""Statement PDF extraction against SYNTHETIC GBM- and DriveWealth-style fixtures.

The fixtures (tests/fixtures/monitoring/*.pdf, rebuilt by
make_statement_fixtures.py) imitate the vocabulary of real statements but are
not copies of one; calibrating against a real statement is still open work.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")

from asset_selection.monitoring.holdings import load_holdings_csv, write_holdings_csv  # noqa: E402
from asset_selection.monitoring.pdf_statement import (  # noqa: E402
    StatementParseError,
    _market_hint,
    extract_holdings_from_pdf,
    match_key,
)

FIX = Path("tests/fixtures/monitoring")


def _by_ticker(lots):
    out = {}
    for lot in lots:
        out.setdefault(lot.ticker, []).append(lot)
    return out


def test_gbm_statement_reads_both_markets_from_section_headings():
    lots, rep = extract_holdings_from_pdf(FIX / "gbm_statement.pdf")
    assert rep.statement_type == "gbm" and rep.confidence == "high"
    got = {(l.ticker, l.market, l.currency) for l in lots}
    assert got == {("NVDA", "SIC", "MXN"), ("AAPL", "SIC", "MXN"),
                   ("WALMEX", "BMV", "MXN"), ("GFNORTEO", "BMV", "MXN")}


def test_cash_rows_are_not_instruments():
    lots, _ = extract_holdings_from_pdf(FIX / "gbm_statement.pdf")
    assert all(l.ticker.upper() != "EFECTIVO" for l in lots)


def test_costs_come_from_the_holdings_table():
    lots = _by_ticker(extract_holdings_from_pdf(FIX / "gbm_statement.pdf")[0])
    assert lots["NVDA"][0].unit_cost == pytest.approx(3850.0)
    assert lots["WALMEX"][0].unit_cost == pytest.approx(62.15)


def test_dates_attach_only_when_buys_explain_the_whole_position():
    lots, rep = extract_holdings_from_pdf(FIX / "gbm_statement.pdf")
    by = _by_ticker(lots)
    (aapl,) = by["AAPL"]                     # 3 shares held, one buy of 3 in the period
    assert aapl.purchase_date == date(2026, 9, 5)
    # The statement's average cost stays authoritative (buy price was 3,380).
    assert aapl.unit_cost == pytest.approx(3400.0)
    (nvda,) = by["NVDA"]                     # 10 held, only 4 bought this period
    assert nvda.purchase_date is None
    assert any("NVDA" in w and "4 of 10" in w for w in rep.warnings)
    assert rep.buy_rows == 2 and rep.dates_attached == 1


def test_table_continued_on_the_next_page_without_a_header():
    lots, rep = extract_holdings_from_pdf(FIX / "gbm_multipage.pdf")
    assert [l.ticker for l in lots] == ["MSFT", "AMZN", "GOOGL", "META"]
    assert all(l.market == "SIC" for l in lots)
    assert [t.kind for t in rep.tables] == ["holdings", "holdings"]


def test_drivewealth_statement_in_usd_with_total_cost_basis():
    lots, rep = extract_holdings_from_pdf(FIX / "drivewealth_statement.pdf")
    by = _by_ticker(lots)
    assert rep.statement_type == "drivewealth"
    assert by["AAPL"][0].unit_cost == pytest.approx(180.20)          # 450.50 / 2.5
    assert by["AAPL"][0].derived_fields == ["unit_cost"]
    assert by["MSFT"][0].purchase_date == date(2026, 9, 3)
    assert all(l.currency == "USD" and l.market == "US" for l in lots)
    assert rep.confidence == "medium"                                # derived values present


def test_no_holdings_table_is_refused_not_guessed():
    with pytest.raises(StatementParseError, match="no holdings table"):
        extract_holdings_from_pdf(FIX / "no_holdings.pdf")


def test_missing_file_is_a_parse_error(tmp_path):
    with pytest.raises(StatementParseError):
        extract_holdings_from_pdf(tmp_path / "nope.pdf")


def test_extraction_round_trips_through_the_review_csv(tmp_path):
    lots, _ = extract_holdings_from_pdf(FIX / "gbm_statement.pdf")
    review = write_holdings_csv(lots, tmp_path / "review.csv")
    again, report = load_holdings_csv(review)
    key = lambda l: (l.ticker, l.market, l.currency, l.shares, round(l.unit_cost, 4), l.purchase_date)
    assert sorted(map(key, again)) == sorted(map(key, lots))
    assert not report.skipped


def test_universe_decides_sic_versus_bmv_when_no_heading_says(tmp_path):
    from asset_selection.monitoring.pdf_statement import _infer_market
    assert _infer_market("NVDA *", None, {"NVDA"}) == ("SIC", True)
    assert _infer_market("WALMEX *", None, {"NVDA"}) == ("BMV", True)
    assert _infer_market("AMX", "B", None) == ("BMV", True)        # series letter = BMV convention


@pytest.mark.parametrize("heading, market", [
    ("ACCIONES SIC - MERCADO GLOBAL", "SIC"),
    ("Acciones nacionales (BMV)", "BMV"),
    # The heading line closest to the table wins over earlier text.
    ("...ACCIONES SIC continued\nACCIONES NACIONALES (BMV)", "BMV"),
    ("Resumen del periodo", None),
])
def test_market_hint(heading, market):
    assert _market_hint(heading) == market


@pytest.mark.parametrize("a, b", [
    (("GFNORTE O",), ("GFNORTE", "O")),
    (("NVDA *",), ("NVDA", "*")),
    (("BRK.B",), ("BRK B",)),
    (("WALMEX.MX",), ("WALMEX *",)),
])
def test_match_key_aligns_broker_spellings(a, b):
    assert match_key(*a) == match_key(*b)
