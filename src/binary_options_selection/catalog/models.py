"""Typed domain model for platform binary-options instruments.

Two different concepts are kept strictly apart in this module:

* the **underlying market instrument** (a stock, ETF, forex pair, index,
  commodity, or cryptocurrency whose price is analyzed), and
* the **binary-options contract** (a platform-specific product with its own
  identifier, availability, expirations, payout, and reference quote).

Market data existing for a symbol never implies a tradable contract exists.
The pipeline only marks a row actionable when a verified catalog entry backs
it; everything else is ``NO_TRADE`` with an explicit reason.

No target platform is integrated yet, so the only working providers read a
**manual, user-supplied catalog** (YAML/CSV). Example entries shipped with
the repository are examples — never live contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from ..utils.timeutils import ensure_utc

# Asset types with a working market-data mapping + session model today.
# Extensible: add a type here only once fetching and session classification
# genuinely support it (see market_session.session_for_asset_type).
SUPPORTED_ASSET_TYPES = (
    "equity",
    "etf",
    "forex",
    "index",
    "commodity",
    "cryptocurrency",
)

CONTRACT_OPEN = "open"
CONTRACT_CLOSED = "closed"
CONTRACT_SUSPENDED = "suspended"
CONTRACT_STATUSES = (CONTRACT_OPEN, CONTRACT_CLOSED, CONTRACT_SUSPENDED)


@dataclass
class BinaryInstrument:
    """One platform-listed binary-options instrument (a tradable contract family)."""

    platform: str
    platform_asset_id: str
    underlying_symbol: str
    asset_type: str

    # Identity / display
    platform_asset_name: Optional[str] = None
    contract_id: Optional[str] = None
    underlying_name: Optional[str] = None
    market: Optional[str] = None            # e.g. NASDAQ, FX, CRYPTO
    currency: Optional[str] = None

    # Market-data mapping (what the intraday provider is asked for).
    provider_symbol: Optional[str] = None

    # Availability
    available_now: bool = True
    contract_status: str = CONTRACT_OPEN
    available_expirations_minutes: List[int] = field(default_factory=list)

    # Contract economics. payout ratio = NET profit per unit staked on a win.
    payout_by_expiration: Dict[int, float] = field(default_factory=dict)
    payout_timestamp: Optional[datetime] = None
    minimum_trade: Optional[float] = None
    maximum_trade: Optional[float] = None

    # Platform-side quote (settlement reference may differ from market data).
    platform_reference_price: Optional[float] = None
    reference_price_timestamp: Optional[datetime] = None
    quote_source: Optional[str] = None

    # Session / venue
    exchange_timezone: Optional[str] = None
    is_otc: bool = False

    def supports_expiration(self, minutes: int) -> bool:
        return int(minutes) in {int(m) for m in self.available_expirations_minutes}

    def payout_for(self, minutes: int) -> Optional[float]:
        v = self.payout_by_expiration.get(int(minutes))
        return float(v) if v is not None else None


@dataclass
class CatalogSnapshot:
    """A loaded instrument catalog plus its provenance and freshness."""

    provider: str                            # catalog provider name (e.g. yaml)
    platform: Optional[str]                  # platform the catalog describes
    path: Optional[str]                      # source file, when file-based
    catalog_timestamp: Optional[datetime]    # when the catalog data was captured
    loaded_at: datetime                      # when this process loaded it
    instruments: List[BinaryInstrument] = field(default_factory=list)
    catalog_hash: Optional[str] = None       # sha256 of the source, when file-based

    def age_seconds(self, now: datetime) -> Optional[float]:
        if self.catalog_timestamp is None:
            return None
        return max(0.0, (ensure_utc(now) - ensure_utc(self.catalog_timestamp)).total_seconds())

    def by_platform_asset_id(self) -> Dict[str, BinaryInstrument]:
        return {i.platform_asset_id: i for i in self.instruments}
