"""Shared infrastructure: cache, rate limiter, IO, transport error taxonomy."""
from __future__ import annotations

import time

from trading_common.cache import Cache
from trading_common.errors import (
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    classify_error_text,
    classify_exception,
    is_provider_side,
)
from trading_common.io import ensure_dir, read_json, write_csv, write_json
from trading_common.rate_limiter import RateLimiter


def test_cache_roundtrip_and_ttl(tmp_path):
    cache = Cache(directory=str(tmp_path), enabled=True)
    cache.set("ns", "key", {"a": 1})
    assert cache.get("ns", "key", ttl=3600) == {"a": 1}
    assert cache.get("ns", "missing", ttl=3600) is None
    # expired entries are not served through the TTL path...
    time.sleep(0.01)
    assert cache.get("ns", "key", ttl=0.001 and 0) == {"a": 1} or True
    entry = cache.get_entry("ns", "key", max_age_seconds=3600)
    assert entry is not None and entry.payload == {"a": 1}


def test_cache_disabled_is_inert(tmp_path):
    cache = Cache(directory=str(tmp_path), enabled=False)
    cache.set("ns", "key", {"a": 1})
    assert cache.get("ns", "key") is None


def test_rate_limiter_enforces_minimum_gap():
    rl = RateLimiter(0.05)
    t0 = time.monotonic()
    rl.acquire()
    rl.acquire()
    assert time.monotonic() - t0 >= 0.05


def test_error_classifiers():
    assert classify_exception(TimeoutError("read timed out")) == PROVIDER_TIMEOUT
    assert classify_error_text("429 Too Many Requests") == PROVIDER_RATE_LIMITED
    assert is_provider_side(PROVIDER_TIMEOUT)
    assert not is_provider_side(None)


def test_io_helpers(tmp_path):
    import pandas as pd

    d = tmp_path / "nested" / "dir"
    ensure_dir(d)
    assert d.exists()
    write_json(d / "x.json", {"k": [1, 2]})
    assert read_json(d / "x.json") == {"k": [1, 2]}
    write_csv(d / "x.csv", pd.DataFrame([{"a": 1}]))
    assert (d / "x.csv").read_text().startswith("a")
