"""Future-outcome (label) definitions — isolated evaluation utility.

This module defines, rigorously, what "the signal was right" will mean when a
future backtesting/calibration milestone evaluates stored signals. It is
**never imported by the live pipeline** (enforced by a test): labels need
candles from *after* the signal timestamp, and keeping that code physically
separate is the simplest guarantee against look-ahead leakage into features.

Definitions, for a signal generated at reference time ``t_ref`` with
reference price ``P_ref`` (the close of the latest completed candle at
signal time) and horizon ``H`` minutes:

* expiration timestamp: ``t_exp = t_ref + H`` (both candle-boundary aligned,
  since horizons are validated as whole interval multiples);
* settlement price: the close of the candle **closing exactly at** ``t_exp``
  (i.e. the candle starting at ``t_exp − interval``). If that candle is
  missing, the outcome is ``UNKNOWN`` — no nearest-candle guessing by
  default. An explicit ``prior_close_tolerance_bars`` may accept the close of
  a candle up to N bars earlier, and any use of it is visible in the result;
* ``CALL_WIN``  if settlement > reference;
* ``PUT_WIN``   if settlement < reference;
* ``TIE``       if settlement == reference (platforms differ on ties; a tie
  is reported as a tie, not silently assigned);
* ``UNKNOWN``   if no acceptable settlement candle exists.

Only candles with ``start >= t_ref`` may be passed in — passing the history
used to generate the signal is rejected, keeping signal data and outcome data
separated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from ..data_providers.base import Candle
from ..utils.timeutils import ensure_utc

CALL_WIN = "CALL_WIN"
PUT_WIN = "PUT_WIN"
TIE = "TIE"
UNKNOWN = "UNKNOWN"


@dataclass
class OutcomeResult:
    outcome: str
    reference_price: float
    settlement_price: Optional[float] = None
    settlement_candle_start: Optional[str] = None
    used_prior_close_bars: int = 0
    detail: Optional[str] = None


def resolve_outcome(
    future_candles: List[Candle],
    *,
    reference_time: datetime,
    reference_price: float,
    expiry_time: datetime,
    interval_seconds: int,
    prior_close_tolerance_bars: int = 0,
) -> OutcomeResult:
    """Resolve the binary outcome from post-signal candles (see module doc)."""
    t_ref = ensure_utc(reference_time)
    t_exp = ensure_utc(expiry_time)
    if t_exp <= t_ref:
        return OutcomeResult(UNKNOWN, reference_price, detail="expiry not after reference")

    for c in future_candles:
        if ensure_utc(c.start) < t_ref:
            raise ValueError(
                "future_candles contains bars before the reference time; "
                "signal-generation data must stay separate from outcome data"
            )

    by_start = {ensure_utc(c.start): c for c in future_candles if c.complete}
    for back in range(0, max(0, int(prior_close_tolerance_bars)) + 1):
        start = t_exp - timedelta(seconds=interval_seconds * (back + 1))
        candle = by_start.get(start)
        if candle is not None:
            settle = candle.close
            outcome = (
                CALL_WIN if settle > reference_price
                else PUT_WIN if settle < reference_price
                else TIE
            )
            return OutcomeResult(
                outcome,
                reference_price,
                settlement_price=settle,
                settlement_candle_start=start.isoformat(),
                used_prior_close_bars=back,
                detail=None if back == 0 else f"settled on close {back} bar(s) early",
            )
    return OutcomeResult(
        UNKNOWN, reference_price,
        detail="no completed candle closes at the expiration timestamp",
    )
