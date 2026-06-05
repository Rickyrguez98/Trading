"""Simple content-addressed on-disk JSON cache.

Each provider stores responses keyed by ``(namespace, identifier)``. A TTL
in seconds is enforced per entry. Disable with ``Cache(enabled=False)``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    timestamp: float
    payload: Any


class Cache:
    def __init__(
        self,
        directory: str = "data/cache",
        enabled: bool = True,
        default_ttl: int = 3600,
    ) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.default_ttl = default_ttl
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _key_path(self, namespace: str, identifier: str) -> Path:
        # Hash the identifier so tickers / URLs become safe filenames.
        digest = hashlib.sha1(identifier.encode("utf-8")).hexdigest()[:24]
        ns_dir = self.directory / namespace
        ns_dir.mkdir(parents=True, exist_ok=True)
        return ns_dir / f"{digest}.json"

    # ------------------------------------------------------------------
    def get(
        self,
        namespace: str,
        identifier: str,
        ttl: Optional[int] = None,
    ) -> Optional[Any]:
        if not self.enabled:
            return None
        path = self._key_path(namespace, identifier)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                blob = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Cache read failed for %s/%s: %s", namespace, identifier, exc)
            return None
        ts = float(blob.get("timestamp", 0.0))
        ttl_seconds = ttl if ttl is not None else self.default_ttl
        if ttl_seconds > 0 and (time.time() - ts) > ttl_seconds:
            return None
        return blob.get("payload")

    # ------------------------------------------------------------------
    def set(self, namespace: str, identifier: str, payload: Any) -> None:
        if not self.enabled:
            return
        path = self._key_path(namespace, identifier)
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump({"timestamp": time.time(), "payload": payload}, f, default=str)
        except OSError as exc:
            logger.warning("Cache write failed for %s/%s: %s", namespace, identifier, exc)

    # ------------------------------------------------------------------
    def invalidate(self, namespace: Optional[str] = None) -> int:
        """Remove cached entries. Returns the number of files deleted."""
        if not self.enabled:
            return 0
        target = self.directory / namespace if namespace else self.directory
        if not target.exists():
            return 0
        count = 0
        for f in target.rglob("*.json"):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
        return count
