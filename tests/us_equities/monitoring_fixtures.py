"""Deterministic builders shared by the portfolio-monitor tests."""
from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from asset_selection.monitoring.holdings import aggregate_lots
from asset_selection.monitoring.models import Holding, Lot

TODAY = date(2026, 10, 13)


def price_frame(
    n: int = 900,
    *,
    start: float = 100.0,
    mu: float = 0.0,
    sigma: float = 0.015,
    seed: int = 1,
    end: date = TODAY,
) -> pd.DataFrame:
    """Geometric random walk on business days ending at ``end``."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(mu, sigma, n)
    closes = start * np.exp(np.cumsum(steps))
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({"close": closes, "adj_close": closes}, index=idx)


def trend_frame(kind: str, n: int = 400, end: date = TODAY) -> pd.DataFrame:
    """Smooth series: 'up' rises steadily, 'down' falls, 'flat' oscillates."""
    t = np.arange(n, dtype=float)
    if kind == "up":
        closes = 100.0 * np.exp(0.0015 * t)
    elif kind == "down":
        closes = 100.0 * np.exp(-0.0015 * t)
    else:
        closes = 100.0 + 2.0 * np.sin(t / 15.0)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({"close": closes, "adj_close": closes}, index=idx)


def constant_frame(value: float, n: int = 400, end: date = TODAY) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame({"close": [value] * n, "adj_close": [value] * n}, index=idx)


def holding(
    ticker: str = "NVDA",
    *,
    shares: float = 10,
    unit_cost: float = 100.0,
    currency: str = "USD",
    market: str = "US",
    purchase_date: Optional[date] = date(2025, 3, 14),
) -> Holding:
    lot = Lot(ticker=ticker, shares=shares, unit_cost=unit_cost, currency=currency,
              market=market, purchase_date=purchase_date, source="test row 1", raw_ticker=ticker)
    return aggregate_lots([lot])[0]
