"""Adaptive throttling: pause on 429, honour Retry-After, stop hammering a blocker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_common import throttle as th
from trading_common.errors import PROVIDER_RATE_LIMITED, classify_exception
from trading_common.throttle import (
    ProviderPaused,
    RateLimitedError,
    Throttle,
    ThrottlePolicy,
    get_throttle,
    is_rate_limited,
    parse_retry_after,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _throttle(policy=None, clock=None):
    slept = []
    t = Throttle("prov", policy or ThrottlePolicy(base_pause=30, max_pause=300, give_up_after=6, cooldown=600),
                 sleep=slept.append, clock=clock or Clock())
    return t, slept


def _flaky(*outcomes):
    """A callable returning/raising the given outcomes in order; counts calls."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        out = outcomes[min(calls["n"], len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out
    return fn, calls


def _429(retry_after=None):
    return RateLimitedError("429 Too Many Requests", retry_after=retry_after)


# ---------------------------------------------------------------------------
# Retry-After parsing / classification
# ---------------------------------------------------------------------------

def test_parse_retry_after_seconds_and_http_date():
    assert parse_retry_after("120") == 120.0
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    later = (now + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(later, now=now) == pytest.approx(90.0)
    assert parse_retry_after(None) is None and parse_retry_after("") is None
    assert parse_retry_after("soon") is None


@pytest.mark.parametrize("exc, expected", [
    (RateLimitedError("x"), True),
    (RuntimeError("429 Client Error: Too Many Requests"), True),
    (RuntimeError("Too Many Requests. Rate limited. Try after a while."), True),   # yfinance's text
    (TimeoutError("read timed out"), False),
    (ValueError("Expecting value: line 1 column 1"), False),
])
def test_is_rate_limited(exc, expected):
    assert is_rate_limited(exc) is expected


def test_a_paused_provider_is_reported_as_rate_limited():
    assert classify_exception(ProviderPaused("yahoo_rss paused after repeated 429 Too Many Requests "
                                             "(rate limited)")) == PROVIDER_RATE_LIMITED


# ---------------------------------------------------------------------------
# Pause and continue
# ---------------------------------------------------------------------------

def test_success_passes_through_without_pausing():
    t, slept = _throttle()
    assert t.call(lambda: 42) == 42 and slept == []


def test_other_errors_are_not_its_business():
    t, slept = _throttle()
    fn, calls = _flaky(TimeoutError("read timed out"))
    with pytest.raises(TimeoutError):
        t.call(fn)
    assert calls["n"] == 1 and slept == []


def test_pauses_then_continues_after_a_rate_limit():
    t, slept = _throttle()
    fn, calls = _flaky(_429(), _429(), "ok")
    assert t.call(fn) == "ok"
    assert calls["n"] == 3
    assert 30 <= slept[0] <= 32 and 60 <= slept[1] <= 62          # 30s, then 60s (+ small jitter)
    assert t.consecutive == 0                                     # success resets the escalation


def test_retry_after_is_honoured():
    t, slept = _throttle()
    fn, _ = _flaky(_429(retry_after=42), "ok")
    assert t.call(fn) == "ok"
    assert 42 <= slept[0] <= 44


def test_an_absurd_retry_after_falls_back_to_backoff():
    t, slept = _throttle(ThrottlePolicy(base_pause=30, max_retry_after=600))
    fn, _ = _flaky(_429(retry_after=86_400), "ok")
    t.call(fn)
    assert slept[0] < 40


def test_pauses_escalate_up_to_the_cap():
    t, slept = _throttle(ThrottlePolicy(base_pause=30, max_pause=100, give_up_after=10))
    fn, _ = _flaky(_429(), _429(), _429(), _429(), "ok")
    t.call(fn)
    for got, expected in zip(slept, [30, 60, 100, 100]):
        assert expected <= got <= expected + 2          # plus at most 2s of jitter


# ---------------------------------------------------------------------------
# Stop hammering a provider that blocks
# ---------------------------------------------------------------------------

def test_repeated_rate_limits_pause_the_provider_and_skip_calls():
    clock = Clock()
    t, slept = _throttle(ThrottlePolicy(base_pause=5, give_up_after=2, cooldown=600), clock=clock)
    fn, calls = _flaky(_429())
    with pytest.raises(RateLimitedError):          # the call that tripped it raises the real error
        t.call(fn)
    assert calls["n"] == 2 and len(slept) == 1
    for _ in range(5):                             # every later call fails fast, nothing is sent
        with pytest.raises(ProviderPaused):
            t.call(fn)
    assert calls["n"] == 2 and t.skipped_calls == 5


def test_after_the_cooldown_one_probe_is_allowed():
    clock = Clock()
    t, _ = _throttle(ThrottlePolicy(base_pause=5, give_up_after=2, cooldown=600), clock=clock)
    fn, calls = _flaky(_429(), _429(), "ok")
    with pytest.raises(RateLimitedError):
        t.call(fn)
    clock.t += 601
    assert t.call(fn) == "ok" and calls["n"] == 3
    assert t.paused_until is None and t.consecutive == 0


def test_one_shared_throttle_per_provider_name():
    assert get_throttle("yfinance") is get_throttle("yfinance")
    assert get_throttle("yfinance") is not get_throttle("finnhub")
    assert get_throttle("yahoo_rss").policy.give_up_after == 2


def test_the_test_suite_never_really_sleeps(_throttle_state):
    t = get_throttle("demo", ThrottlePolicy(base_pause=30))
    fn, _ = _flaky(_429(), "ok")
    t.call(fn)
    assert len(_throttle_state) == 1 and _throttle_state[0] >= 30   # recorded, not slept
    assert th._sleep is not __import__("time").sleep
