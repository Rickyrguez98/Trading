"""Polite request pacing for free / freemium APIs.

A ``RateLimiter`` enforces a minimum gap between successive calls. It is
deliberately simple — a single in-process token bucket. For multi-process
runs you'd swap this for a distributed limiter.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, min_interval_seconds: float = 0.0) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._last_call: float = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: D401 - context-manager hook
        return None
