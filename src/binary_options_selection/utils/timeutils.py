"""UTC-first time handling for intraday candles.

Rules (see docs/binary_options/architecture.md §7):
* every internal timestamp is timezone-aware UTC;
* exchange-local timestamps are derived only at the reporting edge;
* candle boundaries are deterministic: a candle stamped ``t`` covers
  ``[t, t + interval)`` and is **complete** only when ``t + interval <= now``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(ts: datetime) -> datetime:
    """Return ``ts`` as timezone-aware UTC. Naive input is rejected: a naive
    intraday timestamp is ambiguous and silently guessing a zone is exactly the
    kind of dishonesty this module forbids."""
    if ts.tzinfo is None:
        raise ValueError("naive datetime given where an aware one is required")
    return ts.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 string into aware-UTC (accepts trailing 'Z')."""
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no timezone offset")
    return ts.astimezone(timezone.utc)


def iso_utc(ts: datetime) -> str:
    return ensure_utc(ts).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_exchange_local(ts: datetime, tz_name: str) -> datetime:
    return ensure_utc(ts).astimezone(ZoneInfo(tz_name))


def floor_to_interval(ts: datetime, interval_seconds: int) -> datetime:
    """Floor an aware timestamp onto its candle boundary (UTC-based epochs).

    Deterministic: two timestamps within the same candle window always floor
    to the same boundary.
    """
    ts = ensure_utc(ts)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % int(interval_seconds))
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def candle_is_complete(candle_start: datetime, interval_seconds: int, now: datetime) -> bool:
    """A candle stamped ``candle_start`` covers [start, start+interval) and is
    complete only when that window has fully elapsed."""
    return ensure_utc(candle_start) + timedelta(seconds=interval_seconds) <= ensure_utc(now)


def candle_close_time(candle_start: datetime, interval_seconds: int) -> datetime:
    return ensure_utc(candle_start) + timedelta(seconds=interval_seconds)


def data_age_seconds(latest_close: Optional[datetime], now: datetime) -> Optional[float]:
    if latest_close is None:
        return None
    return max(0.0, (ensure_utc(now) - ensure_utc(latest_close)).total_seconds())
