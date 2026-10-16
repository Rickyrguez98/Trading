"""Adaptive throttling for data providers that rate-limit (HTTP 429).

A fixed delay between calls (:class:`trading_common.rate_limiter.RateLimiter`)
keeps a run under a provider's normal limit. This module handles what happens
when the provider says "too many requests" anyway:

* **Pause the whole provider, then continue.** One shared state per provider
  name, so every caller -- health checks, each pipeline stage -- waits out
  the same pause instead of each retrying on its own and making it worse.
* **Wait as long as the provider asks.** A ``Retry-After`` value is honoured
  (up to ``max_retry_after``); otherwise the pause escalates
  ``base_pause, 2x, 4x, ... max_pause``.
* **Stop hammering a provider that is blocking.** After ``give_up_after``
  rate-limited calls in a row the provider is paused for ``cooldown``
  seconds, then a single probe call is allowed. A successful call resets
  everything. While paused, a provider either
  - **waits** (``wait_when_paused``, used for prices and fundamentals): the
    call sleeps until the probe and then carries on, so no ticker is skipped.
    Only after ``max_waits`` cooldowns in a row without a single success --
    a block, not a throttle -- does it stop waiting, so a run cannot hang; or
  - **fails fast** (news): calls raise :class:`ProviderPaused` (classified
    as rate-limited, so the run reports it honestly) without sending anything.

Only rate-limit signals trigger this. Other failures (timeouts, 5xx, parse
errors) go to the caller's own retry policy unchanged.
"""
from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, TypeVar

from .errors import PROVIDER_RATE_LIMITED, classify_exception

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimitedError(RuntimeError):
    """An HTTP 429 from a provider, carrying the server's ``Retry-After`` if any."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderPaused(RuntimeError):
    """The provider is paused after repeated rate limiting; no request was sent."""


@dataclass(frozen=True)
class ThrottlePolicy:
    base_pause: float = 30.0         # first pause after a rate-limited call
    max_pause: float = 300.0         # escalation cap
    give_up_after: int = 6           # rate-limited calls in a row before pausing the provider
    cooldown: float = 600.0          # how long a paused provider is skipped before a probe
    max_retry_after: float = 600.0   # longest server-requested wait honoured
    wait_when_paused: bool = False   # sleep through a cooldown instead of failing fast
    max_waits: int = 3               # cooldowns in a row to wait through before failing fast


def is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, RateLimitedError) or classify_exception(exc) == PROVIDER_RATE_LIMITED


def parse_retry_after(value: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    """``Retry-After`` as seconds: an integer, or an HTTP date. None if absent/garbage."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


class Throttle:
    def __init__(self, name: str, policy: ThrottlePolicy, *,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self.name = name
        self.policy = policy
        self._sleep = sleep
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self.consecutive = 0             # rate-limited calls in a row
        self.paused_until: Optional[float] = None
        self.pauses_taken = 0
        self.skipped_calls = 0
        self.cooldowns = 0               # cooldowns in a row without a successful call

    # ------------------------------------------------------------------
    def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Run ``fn``; on rate limiting, pause and retry; while paused, wait or fail fast."""
        while True:
            wait, until = 0.0, None
            with self._lock:
                if self.paused_until is not None:
                    remaining = self.paused_until - self._clock()
                    if remaining > 0 and not self._may_wait():
                        self.skipped_calls += 1
                        raise ProviderPaused(
                            f"{self.name} paused after repeated 429 Too Many Requests "
                            f"(rate limited); next attempt in {remaining:.0f}s"
                        )
                    if remaining > 0:
                        wait, until = remaining, self.paused_until
                    else:
                        self.paused_until = None     # cooldown over: let one probe through
            if wait:
                self._wait(wait)
                with self._lock:                     # waited it out: this call is the probe
                    if self.paused_until == until:
                        self.paused_until = None
                continue
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - only rate limits are handled here
                if not is_rate_limited(exc):
                    raise
                pause = self._after_rate_limit(exc)
                if pause is None:
                    with self._lock:
                        waiting = self._may_wait()
                    if waiting:
                        continue                     # sleep through the cooldown, then retry this call
                    raise
                self._wait(pause)
                continue
            with self._lock:
                if self.consecutive or self.skipped_calls or self.cooldowns:
                    logger.info("%s is answering again after rate limiting", self.name)
                self.consecutive = 0
                self.skipped_calls = 0
                self.cooldowns = 0
            return result

    def _may_wait(self) -> bool:
        return self.policy.wait_when_paused and self.cooldowns <= self.policy.max_waits

    def _after_rate_limit(self, exc: BaseException) -> Optional[float]:
        """Record a rate-limited call; the pause to take, or None to give up now."""
        p = self.policy
        with self._lock:
            self.consecutive += 1
            if self.consecutive >= p.give_up_after:
                self.paused_until = self._clock() + p.cooldown
                self.consecutive = 0
                self.cooldowns += 1
                if self._may_wait():
                    logger.warning(
                        "%s keeps answering 429 Too Many Requests; waiting %.0fs before trying "
                        "again (no request is sent meanwhile and no ticker is skipped)",
                        self.name, p.cooldown,
                    )
                else:
                    logger.warning(
                        "%s keeps answering 429 Too Many Requests; pausing it for %.0fs and "
                        "skipping its calls meanwhile (no request is sent while paused)",
                        self.name, p.cooldown,
                    )
                return None
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None and 0 < retry_after <= p.max_retry_after:
                pause = float(retry_after)
                why = "as the provider requested (Retry-After)"
            else:
                pause = min(p.max_pause, p.base_pause * (2 ** (self.consecutive - 1)))
                why = "backing off"
            self.pauses_taken += 1
        logger.warning("%s is rate limiting (429); pausing %.0fs %s, then continuing",
                       self.name, pause, why)
        return pause

    def _wait(self, seconds: float) -> None:
        jitter = random.uniform(0.0, min(2.0, seconds * 0.1))
        (self._sleep or _sleep)(seconds + jitter)

    def reset(self) -> None:
        with self._lock:
            self.consecutive = 0
            self.paused_until = None
            self.pauses_taken = 0
            self.skipped_calls = 0
            self.cooldowns = 0


# ---------------------------------------------------------------------------
# One shared throttle per provider name
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: Dict[str, ThrottlePolicy] = {
    # Prices and fundamentals: worth waiting for, so a full run completes every
    # ticker through a temporary throttle: 30s, 60s, 120s, 240s, 300s, then a
    # 10-minute wait, up to three times in a row (about an hour) before
    # concluding Yahoo is blocking and failing the rest fast.
    "yfinance": ThrottlePolicy(base_pause=30.0, max_pause=300.0, give_up_after=6, cooldown=600.0,
                               wait_when_paused=True, max_waits=3),
    "stooq": ThrottlePolicy(base_pause=30.0, max_pause=300.0, give_up_after=6, cooldown=600.0,
                            wait_when_paused=True, max_waits=3),
    # News is best-effort context: a blocked feed is paused quickly instead of
    # costing ~7s of retries per ticker.
    "yahoo_rss": ThrottlePolicy(base_pause=5.0, max_pause=30.0, give_up_after=2, cooldown=600.0),
    # Finnhub's free plan limits calls per minute, so a pause of a minute clears it.
    "finnhub": ThrottlePolicy(base_pause=15.0, max_pause=60.0, give_up_after=4, cooldown=300.0,
                              max_retry_after=120.0),
}

_THROTTLES: Dict[str, Throttle] = {}
_REGISTRY_LOCK = threading.Lock()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def get_throttle(name: str, policy: Optional[ThrottlePolicy] = None) -> Throttle:
    with _REGISTRY_LOCK:
        t = _THROTTLES.get(name)
        if t is None:
            t = Throttle(name, policy or DEFAULT_POLICIES.get(name, ThrottlePolicy()))
            _THROTTLES[name] = t
        return t


def reset_throttles() -> None:
    """Forget all pause state (tests, or a fresh run in a long-lived process)."""
    with _REGISTRY_LOCK:
        _THROTTLES.clear()


def throttle_summary() -> Dict[str, Dict[str, float]]:
    with _REGISTRY_LOCK:
        return {n: {"pauses_taken": t.pauses_taken, "skipped_calls": t.skipped_calls,
                    "paused": t.paused_until is not None}
                for n, t in _THROTTLES.items()}
