"""Instrument-catalog providers.

The catalog interface is deliberately replaceable: a future broker-specific
adapter implements :class:`BinaryInstrumentCatalogProvider` and registers
itself, without touching the pipeline. Until a target platform is specified,
the working providers read a **manual YAML or CSV catalog** maintained by the
user; nothing here invents platform APIs or fabricates availability.

YAML schema (see also configs/binary_options/instruments.example.yaml and
docs/binary_options/instrument-catalog.md):

.. code-block:: yaml

    platform: my_platform
    catalog_timestamp: "2026-07-18T13:00:00Z"   # when YOU captured this data
    instruments:
      - platform_asset_id: AAPL-BIN
        underlying_symbol: AAPL
        provider_symbol: AAPL          # market-data symbol mapping
        asset_type: equity             # equity|etf|forex|index|commodity|cryptocurrency
        available_now: true
        contract_status: open          # open|closed|suspended
        available_expirations_minutes: [5, 10, 15, 30, 60]
        payout_by_expiration: {5: 0.80, 10: 0.82, 15: 0.85, 30: 0.85, 60: 0.87}
        platform_reference_price: 199.42        # optional platform quote
        reference_price_timestamp: "2026-07-18T12:59:55Z"
        exchange_timezone: America/New_York
        is_otc: false

CSV columns mirror the YAML fields; list/dict fields use ``|``-separated
entries (``5|10|15``) and ``expiration:payout`` pairs (``5:0.80|10:0.82``).
"""
from __future__ import annotations

import csv
import hashlib
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..utils.timeutils import parse_utc, utc_now
from .models import BinaryInstrument, CatalogSnapshot

logger = logging.getLogger(__name__)


class CatalogError(ValueError):
    """Raised when a catalog source cannot be read or parsed at all.

    Schema-level problems inside a readable catalog are reported by
    :mod:`binary_options_selection.catalog.validation`, not raised here.
    """


class BinaryInstrumentCatalogProvider(ABC):
    """Replaceable source of platform binary-options instruments."""

    name: str = "base"

    @abstractmethod
    def get_available_instruments(
        self,
        *,
        as_of: Optional[datetime] = None,
        platform: Optional[str] = None,
    ) -> CatalogSnapshot:
        """Return the catalog snapshot; never fabricate instruments."""


def _parse_ts(value: Any, field: str) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        return parse_utc(str(value))
    except ValueError as exc:
        raise CatalogError(f"unparseable {field}: {value!r} ({exc})") from exc


def _instrument_from_mapping(raw: Dict[str, Any]) -> BinaryInstrument:
    expirations = raw.get("available_expirations_minutes") or []
    if isinstance(expirations, str):
        expirations = [e for e in expirations.split("|") if e.strip()]
    payout = raw.get("payout_by_expiration") or {}
    if isinstance(payout, str):
        pairs = [p for p in payout.split("|") if p.strip()]
        payout = {}
        for pair in pairs:
            k, _, v = pair.partition(":")
            payout[k.strip()] = v.strip()
    return BinaryInstrument(
        platform=str(raw.get("platform", "")),
        platform_asset_id=str(raw.get("platform_asset_id", "")).strip(),
        underlying_symbol=str(raw.get("underlying_symbol", "")).strip().upper(),
        asset_type=str(raw.get("asset_type", "")).strip().lower(),
        platform_asset_name=raw.get("platform_asset_name"),
        contract_id=raw.get("contract_id"),
        underlying_name=raw.get("underlying_name"),
        market=raw.get("market"),
        currency=raw.get("currency"),
        provider_symbol=(str(raw["provider_symbol"]).strip()
                         if raw.get("provider_symbol") not in (None, "") else None),
        available_now=_to_bool(raw.get("available_now", True)),
        contract_status=str(raw.get("contract_status", "open")).strip().lower(),
        available_expirations_minutes=[int(e) for e in expirations],
        payout_by_expiration={int(k): float(v) for k, v in dict(payout).items()},
        payout_timestamp=_parse_ts(raw.get("payout_timestamp"), "payout_timestamp"),
        minimum_trade=_to_float(raw.get("minimum_trade")),
        maximum_trade=_to_float(raw.get("maximum_trade")),
        platform_reference_price=_to_float(raw.get("platform_reference_price")),
        reference_price_timestamp=_parse_ts(
            raw.get("reference_price_timestamp"), "reference_price_timestamp"
        ),
        quote_source=raw.get("quote_source"),
        exchange_timezone=raw.get("exchange_timezone"),
        is_otc=_to_bool(raw.get("is_otc", False)),
    )


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    return float(v)


class _FileCatalogProvider(BinaryInstrumentCatalogProvider):
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _read_bytes(self) -> bytes:
        if not self.path.exists():
            raise CatalogError(f"instrument catalog not found: {self.path}")
        return self.path.read_bytes()

    def _snapshot(
        self,
        *,
        platform: Optional[str],
        catalog_timestamp: Optional[datetime],
        instruments: List[BinaryInstrument],
        blob: bytes,
        platform_filter: Optional[str],
    ) -> CatalogSnapshot:
        for inst in instruments:
            if not inst.platform and platform:
                inst.platform = platform
        if platform_filter:
            instruments = [i for i in instruments if i.platform == platform_filter]
        return CatalogSnapshot(
            provider=self.name,
            platform=platform_filter or platform,
            path=str(self.path),
            catalog_timestamp=catalog_timestamp,
            loaded_at=utc_now(),
            instruments=instruments,
            catalog_hash=hashlib.sha256(blob).hexdigest(),
        )


class YamlCatalogProvider(_FileCatalogProvider):
    """Manual YAML catalog (the initial working provider)."""

    name = "yaml"

    def get_available_instruments(
        self, *, as_of: Optional[datetime] = None, platform: Optional[str] = None
    ) -> CatalogSnapshot:
        blob = self._read_bytes()
        try:
            raw = yaml.safe_load(blob) or {}
        except yaml.YAMLError as exc:
            raise CatalogError(f"invalid YAML in {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CatalogError(f"{self.path}: top level must be a mapping")
        instruments = [
            _instrument_from_mapping({**{"platform": raw.get("platform", "")}, **item})
            for item in (raw.get("instruments") or [])
            if isinstance(item, dict)
        ]
        return self._snapshot(
            platform=raw.get("platform"),
            catalog_timestamp=_parse_ts(raw.get("catalog_timestamp"), "catalog_timestamp"),
            instruments=instruments,
            blob=blob,
            platform_filter=platform,
        )


class CsvCatalogProvider(_FileCatalogProvider):
    """Manual CSV catalog. ``catalog_timestamp`` comes from a per-row column
    (the maximum row value is used as the snapshot timestamp)."""

    name = "csv"

    def get_available_instruments(
        self, *, as_of: Optional[datetime] = None, platform: Optional[str] = None
    ) -> CatalogSnapshot:
        blob = self._read_bytes()
        try:
            rows = list(csv.DictReader(blob.decode("utf-8").splitlines()))
        except (csv.Error, UnicodeDecodeError) as exc:
            raise CatalogError(f"invalid CSV in {self.path}: {exc}") from exc
        instruments = [_instrument_from_mapping(r) for r in rows]
        stamps = [
            _parse_ts(r.get("catalog_timestamp"), "catalog_timestamp")
            for r in rows if r.get("catalog_timestamp")
        ]
        catalog_ts = max([s for s in stamps if s is not None], default=None)
        platforms = {i.platform for i in instruments if i.platform}
        return self._snapshot(
            platform=(platforms.pop() if len(platforms) == 1 else None),
            catalog_timestamp=catalog_ts,
            instruments=instruments,
            blob=blob,
            platform_filter=platform,
        )


def build_catalog_provider(path: str, kind: str = "auto") -> BinaryInstrumentCatalogProvider:
    """Build a file catalog provider; ``auto`` picks by extension."""
    kind = (kind or "auto").lower()
    if kind == "auto":
        suffix = Path(path).suffix.lower()
        kind = "csv" if suffix == ".csv" else "yaml"
    if kind == "yaml":
        return YamlCatalogProvider(path)
    if kind == "csv":
        return CsvCatalogProvider(path)
    raise CatalogError(f"unknown catalog provider kind {kind!r} (yaml|csv|auto)")
