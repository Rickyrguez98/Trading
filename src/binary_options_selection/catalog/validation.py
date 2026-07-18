"""Instrument-catalog schema and consistency validation.

``validate_catalog`` never fixes data: it reports **fatal problems** (the
catalog cannot be trusted at all), **warnings** (usable but degraded), and
**per-instrument issues** (that instrument's rows become NO_TRADE with an
explicit reason while the rest of the catalog stays usable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

from ..config import HORIZON_MAX_MINUTES, HORIZON_MIN_MINUTES, InstrumentCatalogConfig
from .models import CONTRACT_STATUSES, SUPPORTED_ASSET_TYPES, CatalogSnapshot

MAX_PLAUSIBLE_PAYOUT = 5.0


@dataclass
class CatalogValidationResult:
    problems: List[str] = field(default_factory=list)        # fatal, catalog-wide
    warnings: List[str] = field(default_factory=list)
    instrument_issues: Dict[str, List[str]] = field(default_factory=dict)
    stale: bool = False
    age_seconds: float | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    def issue(self, asset_id: str, message: str) -> None:
        self.instrument_issues.setdefault(asset_id, []).append(message)

    def to_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "stale": self.stale,
            "age_seconds": self.age_seconds,
            "problems": self.problems,
            "warnings": self.warnings,
            "instrument_issues": self.instrument_issues,
        }


def validate_catalog(
    snapshot: CatalogSnapshot,
    cfg: InstrumentCatalogConfig,
    *,
    now: datetime,
) -> CatalogValidationResult:
    r = CatalogValidationResult()

    if not snapshot.instruments:
        r.problems.append("catalog contains no instruments")
    if snapshot.catalog_timestamp is None:
        r.problems.append(
            "catalog_timestamp is missing -- freshness cannot be verified, so the "
            "catalog cannot back live contract availability"
        )
    else:
        r.age_seconds = snapshot.age_seconds(now)
        if r.age_seconds is not None and r.age_seconds > cfg.max_catalog_age_seconds:
            r.stale = True
            r.warnings.append(
                f"catalog is stale: {r.age_seconds:.0f}s old > allowed "
                f"{cfg.max_catalog_age_seconds:.0f}s -- all rows become "
                "NO_TRADE (CATALOG_STALE)"
            )

    seen_ids: Dict[str, int] = {}
    for inst in snapshot.instruments:
        aid = inst.platform_asset_id or "<missing platform_asset_id>"
        seen_ids[aid] = seen_ids.get(aid, 0) + 1

        if not inst.platform_asset_id:
            r.problems.append("an instrument is missing platform_asset_id")
            continue
        if not inst.platform:
            r.issue(aid, "missing platform")
        if not inst.underlying_symbol:
            r.issue(aid, "missing underlying_symbol")
        if not (inst.provider_symbol or inst.underlying_symbol):
            r.issue(aid, "missing market-data symbol mapping")
        if inst.asset_type not in SUPPORTED_ASSET_TYPES:
            r.issue(
                aid,
                f"unsupported asset_type {inst.asset_type!r} "
                f"(supported: {', '.join(SUPPORTED_ASSET_TYPES)})",
            )
        if inst.contract_status not in CONTRACT_STATUSES:
            r.issue(aid, f"unknown contract_status {inst.contract_status!r}")
        for m in inst.available_expirations_minutes:
            if not (HORIZON_MIN_MINUTES <= int(m) <= HORIZON_MAX_MINUTES):
                r.issue(aid, f"expiration {m}m outside the supported 5-120 range")
        for m, ratio in inst.payout_by_expiration.items():
            if not (0.0 < float(ratio) <= MAX_PLAUSIBLE_PAYOUT):
                r.issue(aid, f"implausible payout {ratio} for {m}m")
            if int(m) not in {int(e) for e in inst.available_expirations_minutes}:
                r.issue(aid, f"payout given for unavailable expiration {m}m")
        if not inst.payout_by_expiration:
            r.warnings.append(
                f"{aid}: no payout data -- its rows require research mode or "
                "become NO_TRADE (PAYOUT_UNKNOWN)"
            )
        if inst.platform_reference_price is not None and inst.platform_reference_price <= 0:
            r.issue(aid, "non-positive platform_reference_price")

    for aid, n in seen_ids.items():
        if n > 1:
            r.problems.append(f"duplicate platform_asset_id: {aid} appears {n} times")

    return r
