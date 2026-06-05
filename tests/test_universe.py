"""Universe cleaning logic."""
from __future__ import annotations

import pandas as pd

from asset_selection.config import UniverseConfig
from asset_selection.universe import clean_universe


def _raw_df() -> pd.DataFrame:
    return pd.DataFrame([
        # Keep
        {"ticker": "AAPL", "company_name": "Apple Inc. - Common Stock",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "MSFT", "company_name": "Microsoft Corporation",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "BRK.B", "company_name": "Berkshire Hathaway Class B",
         "exchange": "NYSE", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        # Drop: ETF flag
        {"ticker": "SPY", "company_name": "SPDR S&P 500 ETF Trust",
         "exchange": "NYSE Arca", "asset_type": "etf", "is_etf": True, "is_test_issue": False},
        # Drop: warrant by name
        {"ticker": "XYZW", "company_name": "Some Company Warrants",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        # Drop: unit by name
        {"ticker": "FOOU", "company_name": "Foo SPAC Units",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        # Drop: preferred share by suffix
        {"ticker": "BAC.PRL", "company_name": "Bank of America 6.50% Preferred Series L",
         "exchange": "NYSE", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        # Drop: rights by name
        {"ticker": "ABCR", "company_name": "Abc Corp Rights",
         "exchange": "NYSE", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        # Drop: test issue
        {"ticker": "ZAZZT", "company_name": "Nasdaq Test Stock",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": True},
        # Drop: invalid ticker
        {"ticker": "", "company_name": "Empty",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
    ])


def test_clean_universe_keeps_common_stocks():
    cleaned = clean_universe(_raw_df(), UniverseConfig())
    tickers = set(cleaned["ticker"])
    assert {"AAPL", "MSFT", "BRK.B"}.issubset(tickers)
    assert "SPY" not in tickers
    assert "XYZW" not in tickers       # warrants
    assert "FOOU" not in tickers       # units
    assert "BAC.PRL" not in tickers    # preferred
    assert "ABCR" not in tickers       # rights
    assert "ZAZZT" not in tickers      # test issue
    assert "" not in tickers           # invalid


def test_clean_universe_respects_disabled_filters():
    cfg = UniverseConfig(exclude_etfs=False)
    cleaned = clean_universe(_raw_df(), cfg)
    # ETF flag suppression is the only one we relaxed -- name-based "Trust"
    # filter for ETFs is also under exclude_etfs, so SPY should now stay.
    assert "SPY" in set(cleaned["ticker"])


def test_clean_universe_handles_empty():
    out = clean_universe(pd.DataFrame(), UniverseConfig())
    assert out.empty


def test_clean_universe_keeps_multiple_exchanges_by_default():
    """The default config must keep tickers from every exchange present in
    the source data, not just NASDAQ."""
    df = pd.DataFrame([
        {"ticker": "AAPL", "company_name": "Apple Inc.",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "JPM", "company_name": "JPMorgan Chase & Co.",
         "exchange": "NYSE", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "IMO", "company_name": "Imperial Oil",
         "exchange": "NYSE American", "asset_type": "common", "is_etf": False, "is_test_issue": False},
    ])
    cleaned = clean_universe(df, UniverseConfig())
    exchanges = set(cleaned["exchange"])
    assert exchanges == {"NASDAQ", "NYSE", "NYSE American"}


def test_clean_universe_exchange_whitelist_restricts_to_subset():
    df = pd.DataFrame([
        {"ticker": "AAPL", "company_name": "Apple Inc.",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "JPM", "company_name": "JPMorgan Chase & Co.",
         "exchange": "NYSE", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "IMO", "company_name": "Imperial Oil",
         "exchange": "NYSE American", "asset_type": "common", "is_etf": False, "is_test_issue": False},
    ])
    cfg = UniverseConfig(exchanges=["NYSE"])
    cleaned = clean_universe(df, cfg)
    assert set(cleaned["ticker"]) == {"JPM"}


def test_clean_universe_exchange_alias_amex_resolves_to_nyse_american():
    df = pd.DataFrame([
        {"ticker": "IMO", "company_name": "Imperial Oil",
         "exchange": "NYSE American", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "AAPL", "company_name": "Apple Inc.",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
    ])
    cfg = UniverseConfig(exchanges=["AMEX"])
    cleaned = clean_universe(df, cfg)
    assert set(cleaned["ticker"]) == {"IMO"}


def test_clean_universe_include_etfs_toggle_keeps_etfs():
    df = pd.DataFrame([
        {"ticker": "AAPL", "company_name": "Apple Inc.",
         "exchange": "NASDAQ", "asset_type": "common", "is_etf": False, "is_test_issue": False},
        {"ticker": "SPY", "company_name": "SPDR S&P 500 ETF Trust",
         "exchange": "NYSE Arca", "asset_type": "etf", "is_etf": True, "is_test_issue": False},
    ])
    # New include_etfs=True keeps SPY (and we have to drop legacy exclude_etfs)
    cfg = UniverseConfig(include_etfs=True)
    cleaned = clean_universe(df, cfg)
    assert "SPY" in set(cleaned["ticker"])
    assert "AAPL" in set(cleaned["ticker"])
