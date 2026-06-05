"""Sources of U.S.-listed tickers.

Primary: NASDAQ Trader FTP-served pipe-delimited symbol directories.
Fallback: SEC company tickers JSON (no exchange / no asset type, used only
as a last resort).
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import DataProvider

logger = logging.getLogger(__name__)


@dataclass
class TickerRecord:
    ticker: str
    company_name: Optional[str] = None
    exchange: Optional[str] = None
    asset_type: Optional[str] = None      # e.g. 'common', 'etf', 'preferred', ...
    is_etf: bool = False
    is_test_issue: bool = False


# ---------------------------------------------------------------------------
# NASDAQ Trader (primary)
# ---------------------------------------------------------------------------

_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_REQUEST_HEADERS = {
    "User-Agent": "asset-selection-research/0.1 (+research; contact via repo)"
}


class NasdaqTraderTickerProvider(DataProvider):
    """Pulls nasdaqlisted.txt + otherlisted.txt and yields TickerRecord rows."""

    name = "nasdaq_trader"
    cache_namespace = "universe"

    def fetch_all(self) -> List[TickerRecord]:
        cached = self._cache_get("nasdaq_trader_full")
        if cached is not None:
            logger.info("Loaded NASDAQ Trader universe from cache (%d rows).", len(cached))
            return [TickerRecord(**row) for row in cached]

        nasdaq_df = self._fetch_pipe_table(_NASDAQ_URL)
        other_df = self._fetch_pipe_table(_OTHER_URL)
        records = self._parse_nasdaq(nasdaq_df) + self._parse_other(other_df)

        self._cache_set("nasdaq_trader_full", [r.__dict__ for r in records])
        logger.info("Fetched %d raw symbols from NASDAQ Trader.", len(records))
        return records

    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    def _fetch_pipe_table(self, url: str) -> pd.DataFrame:
        self.rate_limiter.acquire()
        logger.debug("GET %s", url)
        resp = requests.get(url, headers=_REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        text = resp.text
        # Files end with a 'File Creation Time:' footer line. Strip it.
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
        if len(lines) < 2:
            raise RuntimeError(f"Empty or malformed NASDAQ Trader file at {url}")
        cleaned = "\n".join(lines)
        return pd.read_csv(io.StringIO(cleaned), sep="|", dtype=str).fillna("")

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_nasdaq(df: pd.DataFrame) -> List[TickerRecord]:
        """Schema: Symbol | Security Name | Market Category | Test Issue | Financial Status | Round Lot Size | ETF | NextShares"""
        if df.empty:
            return []
        out: List[TickerRecord] = []
        for _, row in df.iterrows():
            symbol = str(row.get("Symbol", "")).strip()
            if not symbol:
                continue
            etf_flag = str(row.get("ETF", "")).strip().upper() == "Y"
            test_flag = str(row.get("Test Issue", "")).strip().upper() == "Y"
            out.append(
                TickerRecord(
                    ticker=symbol,
                    company_name=str(row.get("Security Name", "")).strip() or None,
                    exchange="NASDAQ",
                    asset_type="etf" if etf_flag else "common",
                    is_etf=etf_flag,
                    is_test_issue=test_flag,
                )
            )
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_other(df: pd.DataFrame) -> List[TickerRecord]:
        """Schema: ACT Symbol | Security Name | Exchange | CQS Symbol | ETF | Round Lot Size | Test Issue | NASDAQ Symbol"""
        if df.empty:
            return []
        exchange_map = {
            "A": "NYSE American",
            "N": "NYSE",
            "P": "NYSE Arca",
            "Z": "BATS",
            "V": "IEX",
        }
        out: List[TickerRecord] = []
        for _, row in df.iterrows():
            symbol = str(row.get("ACT Symbol", "") or row.get("NASDAQ Symbol", "")).strip()
            if not symbol:
                continue
            etf_flag = str(row.get("ETF", "")).strip().upper() == "Y"
            test_flag = str(row.get("Test Issue", "")).strip().upper() == "Y"
            ex_code = str(row.get("Exchange", "")).strip().upper()
            out.append(
                TickerRecord(
                    ticker=symbol,
                    company_name=str(row.get("Security Name", "")).strip() or None,
                    exchange=exchange_map.get(ex_code, ex_code or "OTHER"),
                    asset_type="etf" if etf_flag else "common",
                    is_etf=etf_flag,
                    is_test_issue=test_flag,
                )
            )
        return out


# ---------------------------------------------------------------------------
# SEC company tickers (fallback)
# ---------------------------------------------------------------------------

_SEC_URL = "https://www.sec.gov/files/company_tickers.json"


class SECCompanyTickersProvider(DataProvider):
    """Fallback: SEC company tickers JSON. No exchange / no asset type info."""

    name = "sec_company_tickers"
    cache_namespace = "universe"

    def fetch_all(self) -> List[TickerRecord]:
        cached = self._cache_get("sec_company_tickers_full")
        if cached is not None:
            logger.info("Loaded SEC company tickers from cache (%d rows).", len(cached))
            return [TickerRecord(**row) for row in cached]

        self.rate_limiter.acquire()
        logger.debug("GET %s", _SEC_URL)
        resp = requests.get(_SEC_URL, headers=_REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records: List[TickerRecord] = []
        for _, entry in data.items():
            ticker = str(entry.get("ticker", "")).strip().upper()
            name = str(entry.get("title", "")).strip()
            if not ticker:
                continue
            records.append(
                TickerRecord(
                    ticker=ticker,
                    company_name=name or None,
                    exchange=None,
                    asset_type=None,
                )
            )
        self._cache_set("sec_company_tickers_full", [r.__dict__ for r in records])
        logger.info("Fetched %d symbols from SEC company tickers.", len(records))
        return records
