"""Value each holding in its own currency and in the reporting currency.

Two price conventions, used deliberately:

* **Raw close** values the position -- it is what the broker shows.
* **Adjusted close** (dividends and splits folded in) measures returns over
  time, so a dividend is not mistaken for a price drop.

For an SIC position (a US listing carried in MXN) the MXN value is the US price
times USDMXN. The broker's own SIC quote tracks that closely (arbitrage keeps
them aligned) but can differ by a small spread; the report states which rate
was used.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from .market_data import PriceHistory
from .models import Holding, PositionReport, Valuation


def conversion(from_ccy: str, to_ccy: str, usdmxn: Optional[float]) -> Optional[float]:
    """Multiplier turning an amount in ``from_ccy`` into ``to_ccy`` (None if FX missing)."""
    if from_ccy == to_ccy:
        return 1.0
    if usdmxn is None or usdmxn <= 0:
        return None
    if from_ccy == "USD" and to_ccy == "MXN":
        return usdmxn
    if from_ccy == "MXN" and to_ccy == "USD":
        return 1.0 / usdmxn
    return None


def compute_valuation(
    holding: Holding,
    prices: PriceHistory,
    fx: Optional[PriceHistory],
    reporting_currency: str,
    *,
    today: date,
) -> Valuation:
    v = Valuation()
    if not prices.ok:
        return v
    last = prices.last_close
    last_day = prices.last_date
    v.last_price = last
    v.last_price_date = last_day.isoformat() if last_day else None
    v.price_age_days = (today - last_day).days if last_day else None
    v.price_source = prices.source
    usdmxn = fx.last_close if (fx is not None and fx.ok) else None
    v.fx_rate = usdmxn

    to_position = conversion(holding.listing_currency, holding.currency, usdmxn)
    if to_position is None or last is None:
        return v                                  # caller flags FX_UNAVAILABLE
    cost = holding.cost_basis
    v.market_value = holding.shares * last * to_position
    v.unrealized_pl = v.market_value - cost
    v.unrealized_pl_pct = 100.0 * (v.market_value / cost - 1.0) if cost > 0 else None

    to_reporting = conversion(holding.currency, reporting_currency, usdmxn)
    if to_reporting is not None:
        v.market_value_reporting = v.market_value * to_reporting
        v.cost_basis_reporting = cost * to_reporting
        v.unrealized_pl_reporting = v.unrealized_pl * to_reporting

    first = holding.first_purchase_date
    if first is not None:
        v.holding_days = (today - first).days
        _since_purchase(v, prices, first)

    if (holding.currency == "MXN" and holding.listing_currency == "USD"
            and holding.dates_complete and fx is not None and fx.ok):
        _split_stock_and_fx(v, holding, last, fx)
    return v


def _since_purchase(v: Valuation, prices: PriceHistory, first: date) -> None:
    """Peak gain and drawdown of the instrument since the first purchase."""
    import pandas as pd

    adj = prices.frame["adj_close"]
    path = adj.loc[pd.Timestamp(first):]
    before = adj.loc[: pd.Timestamp(first)]
    if path.empty or before.empty:
        return
    entry = float(before.iloc[-1])
    peak = float(path.max())
    now = float(path.iloc[-1])
    if entry > 0 and peak > 0:
        v.max_gain_since_purchase_pct = 100.0 * (peak / entry - 1.0)
        v.drawdown_from_peak_since_purchase_pct = 100.0 * (now / peak - 1.0)


def _split_stock_and_fx(v: Valuation, holding: Holding, last_usd: float, fx: PriceHistory) -> None:
    """MXN return = (1 + stock return in USD) * (1 + USDMXN return) - 1.

    Each lot's MXN cost is converted to USD at the USDMXN close on its own
    purchase date, so multi-lot positions split exactly rather than through a
    single averaged rate.
    """
    usd_cost = 0.0
    for lot in holding.lots:
        rate = fx.close_on_or_before(lot.purchase_date) if lot.purchase_date else None
        if rate is None or rate <= 0:
            return
        usd_cost += lot.cost / rate
    if usd_cost <= 0 or holding.cost_basis <= 0 or v.market_value is None:
        return
    stock = holding.shares * last_usd / usd_cost - 1.0
    total = v.market_value / holding.cost_basis - 1.0
    v.stock_return_pct = 100.0 * stock
    v.fx_return_pct = 100.0 * ((1.0 + total) / (1.0 + stock) - 1.0)


def assign_weights(reports: List[PositionReport]) -> float:
    """Set each position's portfolio weight; return the total value (reporting ccy)."""
    total = sum(r.valuation.market_value_reporting or 0.0 for r in reports)
    for r in reports:
        mv = r.valuation.market_value_reporting
        r.valuation.weight = (mv / total) if (mv is not None and total > 0) else None
    return total
