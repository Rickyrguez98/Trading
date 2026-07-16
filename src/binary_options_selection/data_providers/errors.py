"""Intraday-specific error taxonomy, extending the shared transport taxonomy.

Generic transport faults (rate limits, timeouts, parse errors, ...) come from
:mod:`trading_common.errors` and are re-exported so callers need one import.
"""
from __future__ import annotations

from trading_common.errors import (  # noqa: F401  (re-exported)
    OK,
    PROVIDER_BLOCKED,
    PROVIDER_EMPTY_RESPONSE,
    PROVIDER_HTTP_ERROR,
    PROVIDER_JSON_PARSE_ERROR,
    PROVIDER_RATE_LIMITED,
    PROVIDER_SIDE_ERRORS,
    PROVIDER_TIMEOUT,
    PROVIDER_UNKNOWN_ERROR,
    classify_error_text,
    classify_exception,
    is_provider_side,
)

# Honest "the call worked but there is no usable intraday data" outcomes.
NO_INTRADAY_DATA = "NO_INTRADAY_DATA"
UNSUPPORTED_INTERVAL = "UNSUPPORTED_INTERVAL"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

# Data-integrity outcomes detected by validation (not transport failures).
STALE_INTRADAY_DATA = "STALE_INTRADAY_DATA"
CORRUPT_CANDLE_DATA = "CORRUPT_CANDLE_DATA"

EMPTY_DATA_ERRORS = frozenset({
    NO_INTRADAY_DATA,
    INSUFFICIENT_HISTORY,
    PROVIDER_EMPTY_RESPONSE,
})


def status_for(error_type: str | None) -> str:
    """Collapse an error type to the coarse ``status`` field ("ok"/"empty"/"error")."""
    if error_type in (None, OK):
        return "ok"
    if error_type in EMPTY_DATA_ERRORS:
        return "empty"
    return "error"
