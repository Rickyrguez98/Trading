"""Backward-compatible shim: the cache now lives in ``trading_common.cache``.

Kept so every existing ``asset_selection.utils.cache`` import keeps working.
"""
from trading_common.cache import Cache, CacheEntry  # noqa: F401
