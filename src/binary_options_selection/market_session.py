"""Exchange market-session classification and horizon/expiry alignment.

Sessions are classified structurally from the exchange-local wall clock:
weekday + configured session windows. There is deliberately **no exchange
holiday calendar** in this milestone (documented limitation): on a holiday the
clock says "regular hours" but no fresh candles arrive, so the staleness gate
(``STALE_DATA``) is what actually protects the signal. ``MARKET_STATE_UNKNOWN``
is reserved for timezone/config failures — the honest answer when we cannot
classify.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MarketSessionConfig
from .utils.timeutils import ensure_utc

MARKET_OPEN = "open"                 # regular session
MARKET_PRE = "pre_market"
MARKET_AFTER = "after_hours"
MARKET_CLOSED = "closed"
MARKET_UNKNOWN = "unknown"


@dataclass
class SessionState:
    state: str                       # one of the MARKET_* constants
    exchange_local_time: Optional[str] = None
    timezone: Optional[str] = None
    # Whether the configured policy allows live analysis in this state.
    allowed_for_analysis: bool = False
    detail: Optional[str] = None


def _parse_hhmm(value: str) -> time:
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


def classify_session(now: datetime, cfg: MarketSessionConfig) -> SessionState:
    """Classify ``now`` (aware UTC) into a market-session state under ``cfg``."""
    try:
        local = ensure_utc(now).astimezone(ZoneInfo(cfg.timezone))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return SessionState(
            state=MARKET_UNKNOWN,
            timezone=cfg.timezone,
            allowed_for_analysis=False,
            detail=f"cannot resolve session: {exc}",
        )

    local_iso = local.isoformat(timespec="seconds")
    if local.weekday() >= 5:  # Saturday/Sunday
        return SessionState(MARKET_CLOSED, local_iso, cfg.timezone, False, "weekend")

    t = local.time()
    regular_open = _parse_hhmm(cfg.regular_open)
    regular_close = _parse_hhmm(cfg.regular_close)
    pre_open = _parse_hhmm(cfg.premarket_open)
    after_close = _parse_hhmm(cfg.afterhours_close)

    if regular_open <= t < regular_close:
        state = MARKET_OPEN
        allowed = True
    elif pre_open <= t < regular_open:
        state = MARKET_PRE
        allowed = cfg.session == "extended" and cfg.allow_premarket
    elif regular_close <= t < after_close:
        state = MARKET_AFTER
        allowed = cfg.session == "extended" and cfg.allow_afterhours
    else:
        state = MARKET_CLOSED
        allowed = False

    if not cfg.require_open_market:
        # Research mode: analysis is allowed, but the state is still reported
        # honestly so a closed-market row can never masquerade as live.
        allowed = True

    return SessionState(state, local_iso, cfg.timezone, allowed, None)


def expiry_for(reference_time: datetime, horizon_minutes: int) -> datetime:
    """Intended expiration timestamp: reference time + horizon.

    The reference time is the close of the latest completed candle, which is
    already aligned to a candle boundary; horizons are validated to be whole
    multiples of the interval, so the expiry also lands on a boundary.
    """
    return ensure_utc(reference_time) + timedelta(minutes=int(horizon_minutes))
