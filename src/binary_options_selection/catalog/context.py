"""Per-(instrument, horizon) contract context for the decision layer.

``build_contract_context`` evaluates everything the catalog can say about one
row — availability, contract status, expiration availability, OTC policy,
payout economics, and platform-vs-market quote alignment — into a flat,
JSON-friendly structure the decision gates and the output schema both
consume. It never fabricates: missing information stays ``None`` and turns
into an explicit gate, not a default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ..config import InstrumentCatalogConfig
from ..utils.timeutils import ensure_utc, iso_utc
from .models import CONTRACT_OPEN, BinaryInstrument, CatalogSnapshot

QUOTE_ALIGNED = "aligned"
QUOTE_MISMATCH = "mismatch"
QUOTE_STALE = "stale"
QUOTE_UNAVAILABLE = "unavailable"


@dataclass
class ContractContext:
    """What the verified catalog says about one (instrument, horizon) pair."""

    availability_known: bool = False       # a verified catalog backs this row
    research_mode: bool = False

    instrument: Optional[BinaryInstrument] = None
    catalog_provider: Optional[str] = None
    catalog_age_seconds: Optional[float] = None
    catalog_stale: bool = False

    contract_available: bool = False       # instrument listed + available_now
    contract_open: bool = False
    expiration_available: bool = False
    otc_excluded: bool = False

    # Contract economics (payout ratio = net profit per unit staked).
    payout_ratio: Optional[float] = None
    payout_source: Optional[str] = None
    payout_timestamp: Optional[str] = None
    payout_age_seconds: Optional[float] = None
    payout_stale: bool = False
    payout_below_minimum: bool = False

    # Platform-vs-market-data quote alignment.
    platform_reference_price: Optional[float] = None
    platform_quote_age_seconds: Optional[float] = None
    absolute_quote_difference: Optional[float] = None
    relative_quote_difference_bps: Optional[float] = None
    quote_alignment_status: str = QUOTE_UNAVAILABLE

    warnings: List[str] = field(default_factory=list)


def unknown_contract_context(*, research_mode: bool) -> ContractContext:
    """Context when no verified catalog backs the row (the default state)."""
    return ContractContext(availability_known=False, research_mode=research_mode)


def build_contract_context(
    instrument: Optional[BinaryInstrument],
    *,
    snapshot: Optional[CatalogSnapshot],
    catalog_stale: bool,
    horizon_minutes: int,
    cfg: InstrumentCatalogConfig,
    research_mode: bool,
    market_reference_price: Optional[float],
    now: datetime,
) -> ContractContext:
    ctx = ContractContext(research_mode=research_mode)
    if snapshot is None or instrument is None:
        return ctx  # availability unknown

    ctx.availability_known = True
    ctx.instrument = instrument
    ctx.catalog_provider = snapshot.provider
    ctx.catalog_age_seconds = snapshot.age_seconds(now)
    ctx.catalog_stale = catalog_stale

    ctx.contract_available = bool(instrument.available_now)
    ctx.contract_open = instrument.contract_status == CONTRACT_OPEN
    ctx.expiration_available = instrument.supports_expiration(horizon_minutes)
    ctx.otc_excluded = bool(instrument.is_otc and cfg.exclude_otc)

    # --- payout ---
    ratio = instrument.payout_for(horizon_minutes)
    if ratio is not None:
        ctx.payout_ratio = ratio
        ctx.payout_source = instrument.quote_source or instrument.platform or snapshot.provider
        ts = instrument.payout_timestamp or snapshot.catalog_timestamp
        if ts is not None:
            ctx.payout_timestamp = iso_utc(ts)
            ctx.payout_age_seconds = max(
                0.0, (ensure_utc(now) - ensure_utc(ts)).total_seconds()
            )
            ctx.payout_stale = ctx.payout_age_seconds > cfg.maximum_payout_age_seconds
        ctx.payout_below_minimum = ratio < cfg.minimum_acceptable_payout

    # --- platform quote alignment ---
    ctx.platform_reference_price = instrument.platform_reference_price
    if instrument.platform_reference_price is not None:
        qts = instrument.reference_price_timestamp or snapshot.catalog_timestamp
        if qts is not None:
            ctx.platform_quote_age_seconds = max(
                0.0, (ensure_utc(now) - ensure_utc(qts)).total_seconds()
            )
        if (
            ctx.platform_quote_age_seconds is not None
            and ctx.platform_quote_age_seconds > cfg.max_platform_quote_age_seconds
        ):
            ctx.quote_alignment_status = QUOTE_STALE
        elif market_reference_price is not None and market_reference_price > 0:
            diff = abs(instrument.platform_reference_price - market_reference_price)
            ctx.absolute_quote_difference = round(diff, 6)
            ctx.relative_quote_difference_bps = round(
                10_000.0 * diff / market_reference_price, 2
            )
            ctx.quote_alignment_status = (
                QUOTE_MISMATCH
                if ctx.relative_quote_difference_bps > cfg.max_quote_diff_bps
                else QUOTE_ALIGNED
            )
        else:
            ctx.quote_alignment_status = QUOTE_UNAVAILABLE
    return ctx
