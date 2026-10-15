"""Holdings input: number/date conventions, ticker resolution, CSV loading, lots."""
from __future__ import annotations

from datetime import date

import pytest

from asset_selection.monitoring.holdings import (
    HoldingsError,
    aggregate_lots,
    canonical_field,
    load_holdings_csv,
    parse_date,
    parse_number,
    resolve_instrument,
    write_holdings_csv,
)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("3,850.25", 3850.25),
    ("$ 3,850.25 MXN", 3850.25),
    ("3.850,25", 3850.25),          # European order still parses
    ("1,234", 1234.0),              # Mexican thousands separator
    ("1,234,567.5", 1234567.5),
    ("12,5", 12.5),                 # decimal comma
    ("0,123", 0.123),               # never read as 123
    ("1.234.567", 1234567.0),
    ("(12.50)", -12.5),
    ("-7", -7.0),
    (42, 42.0),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "—", "n/a", "nan", float("nan"), "abc"])
def test_parse_number_returns_none_for_blanks_and_garbage(raw):
    assert parse_number(raw) is None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("2025-03-14", date(2025, 3, 14)),
    ("2025-03-14 00:00:00", date(2025, 3, 14)),
    ("14/03/2025", date(2025, 3, 14)),       # day-first, the Mexican convention
    ("03/04/2025", date(2025, 4, 3)),        # ...so this is 3 April, never 4 March
    ("14-03-25", date(2025, 3, 14)),
    ("14/MAR/2025", date(2025, 3, 14)),
    ("14-ene-2025", date(2025, 1, 14)),      # Spanish month abbreviations
    ("5 DIC 24", date(2024, 12, 5)),
    ("1-ago-2025", date(2025, 8, 1)),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_date_blank_is_none():
    assert parse_date("") is None and parse_date(None) is None


@pytest.mark.parametrize("bad", ["2025/31/12", "31/31/2025", "yesterday", "14/foo/2025"])
def test_parse_date_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_date(bad)


# ---------------------------------------------------------------------------
# Header vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header, field", [
    ("Emisora", "ticker"), ("Títulos", "shares"), ("TITULOS ACTUALES", "shares"),
    ("Costo Promedio", "unit_cost"), ("Costo promedio MXN", "unit_cost"),
    ("Costo Total", "total_cost"), ("Precio de Mercado", "market_price"),
    ("Valor de Mercado", "market_value"), ("Fecha de Operación", "purchase_date"),
    ("Symbol", "ticker"), ("Quantity", "shares"), ("Cost Basis", "total_cost"),
    ("avg_cost", "unit_cost"), ("Serie", "series"), ("Moneda", "currency"),
])
def test_canonical_field(header, field):
    assert canonical_field(header) == field


def test_unknown_header_maps_to_nothing():
    assert canonical_field("Comisiones e IVA") is None


# ---------------------------------------------------------------------------
# Ticker / market resolution
# ---------------------------------------------------------------------------

def test_sic_position_is_a_us_listing_carried_in_mxn():
    inst = resolve_instrument("NVDA *", market="SIC", currency="MXN")
    assert (inst.ticker, inst.market, inst.currency) == ("NVDA", "SIC", "MXN")
    assert inst.analysis_symbol == "NVDA" and inst.listing_currency == "USD"
    assert inst.in_selection_coverage


def test_bmv_listing_uses_the_yahoo_mx_symbol_with_its_series():
    inst = resolve_instrument("GFNORTE O", market="BMV")
    assert inst.ticker == "GFNORTEO" and inst.analysis_symbol == "GFNORTEO.MX"
    assert inst.currency == "MXN" and inst.listing_currency == "MXN"
    assert not inst.in_selection_coverage
    assert resolve_instrument("WALMEX *", market="BMV").analysis_symbol == "WALMEX.MX"


@pytest.mark.parametrize("raw", ["BRK.B", "BRK-B", "BRK B"])
def test_class_shares_in_any_spelling(raw):
    inst = resolve_instrument(raw, currency="USD")
    assert inst.ticker == "BRK.B" and inst.analysis_symbol == "BRK-B"


def test_mxn_without_market_is_assumed_sic_and_says_so():
    inst = resolve_instrument("AAPL", currency="MXN")
    assert inst.market == "SIC"
    assert any("MARKET_ASSUMED_SIC" in w for w in inst.warnings)


def test_us_market_in_mxn_is_corrected_to_sic():
    inst = resolve_instrument("AAPL", market="US", currency="MXN")
    assert inst.market == "SIC"
    assert any("MARKET_SET_TO_SIC" in w for w in inst.warnings)


def test_mx_suffix_implies_bmv():
    assert resolve_instrument("WALMEX.MX").market == "BMV"


def test_bmv_in_usd_is_rejected():
    with pytest.raises(HoldingsError):
        resolve_instrument("WALMEX", market="BMV", currency="USD")


@pytest.mark.parametrize("kw", [{"market": "NYSE"}, {"currency": "EUR"}])
def test_unknown_market_or_currency_rejected(kw):
    with pytest.raises(HoldingsError):
        resolve_instrument("AAPL", **kw)


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def _csv(tmp_path, text):
    p = tmp_path / "holdings.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_shipped_template_loads():
    lots, report = load_holdings_csv("configs/us_equities/holdings_template.csv")
    assert {l.ticker for l in lots} == {"NVDA", "AAPL", "WALMEX"}
    nvda = next(l for l in lots if l.ticker == "NVDA")
    assert nvda.market == "SIC" and nvda.currency == "MXN" and nvda.purchase_date == date(2025, 3, 14)
    walmex = next(l for l in lots if l.ticker == "WALMEX")
    assert walmex.purchase_date == date(2025, 1, 14)     # 14/01/2025, day-first


def test_spanish_headers_and_total_cost_are_understood(tmp_path):
    p = _csv(tmp_path, "Emisora,Serie,Títulos,Costo Total,Moneda,Mercado\n"
                       "GFNORTE,O,100,\"15,000.00\",MXN,BMV\n")
    lots, report = load_holdings_csv(p)
    (lot,) = lots
    assert lot.ticker == "GFNORTEO" and lot.unit_cost == pytest.approx(150.0)
    assert lot.derived_fields == ["unit_cost"]
    assert report.column_mapping["total_cost"] == "Costo Total"


def test_rows_without_cost_are_skipped_never_guessed(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency\nAAPL,5,,USD\nMSFT,3,410.5,USD\n")
    lots, report = load_holdings_csv(p)
    assert [l.ticker for l in lots] == ["MSFT"]
    assert any("AAPL" in s and "no positive cost" in s for s in report.skipped)


def test_cash_and_total_rows_are_not_instruments(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency\nTOTAL,1,1,USD\nEfectivo,1,1,MXN\nMSFT,3,410,USD\n")
    lots, report = load_holdings_csv(p)
    assert [l.ticker for l in lots] == ["MSFT"]
    assert len(report.skipped) == 2


def test_bad_or_future_dates_are_blanked_with_a_warning(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency,purchase_date\n"
                       "MSFT,3,410,USD,someday\nAAPL,1,200,USD,2099-01-01\n")
    lots, report = load_holdings_csv(p)
    assert all(l.purchase_date is None for l in lots)
    assert len(report.warnings) == 2


def test_missing_required_columns_is_an_error(tmp_path):
    p = _csv(tmp_path, "ticker,avg_cost\nAAPL,100\n")
    with pytest.raises(HoldingsError, match="shares"):
        load_holdings_csv(p)


def test_no_usable_rows_is_an_error(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost\nAAPL,0,100\n")
    with pytest.raises(HoldingsError, match="no usable"):
        load_holdings_csv(p)


# ---------------------------------------------------------------------------
# Lots -> holdings
# ---------------------------------------------------------------------------

def test_lots_aggregate_with_a_share_weighted_average_cost(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency,purchase_date\n"
                       "MSFT,10,100,USD,2025-01-10\nMSFT,30,200,USD,2025-06-10\n")
    (h,) = aggregate_lots(load_holdings_csv(p)[0])
    assert h.shares == 40 and h.cost_basis == pytest.approx(7000.0)
    assert h.avg_cost == pytest.approx(175.0)
    assert h.first_purchase_date == date(2025, 1, 10) and h.last_purchase_date == date(2025, 6, 10)
    assert h.dates_complete


def test_same_ticker_in_two_markets_stays_two_positions(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency,market\n"
                       "NVDA,5,3800,MXN,SIC\nNVDA,2,190,USD,US\n")
    holdings = aggregate_lots(load_holdings_csv(p)[0])
    assert sorted(h.key for h in holdings) == ["NVDA|SIC|MXN", "NVDA|US|USD"]


def test_missing_dates_and_bmv_coverage_are_flagged(tmp_path):
    p = _csv(tmp_path, "ticker,shares,avg_cost,currency,market\nWALMEX *,40,62,MXN,BMV\n")
    (h,) = aggregate_lots(load_holdings_csv(p)[0])
    assert any(w.startswith("PURCHASE_DATE_MISSING") for w in h.warnings)
    assert any(w.startswith("OUTSIDE_SELECTION_COVERAGE") for w in h.warnings)


def test_review_csv_round_trips(tmp_path):
    lots, _ = load_holdings_csv("configs/us_equities/holdings_template.csv")
    out = write_holdings_csv(lots, tmp_path / "review.csv")
    again, _ = load_holdings_csv(out)
    assert [(l.ticker, l.shares, round(l.unit_cost, 4), l.market, l.purchase_date) for l in again] == \
           [(l.ticker, l.shares, round(l.unit_cost, 4), l.market, l.purchase_date) for l in lots]
