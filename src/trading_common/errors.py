"""Generic, domain-free provider error taxonomy + classifiers.

These are the transport-level outcomes every external data provider can hit
regardless of domain (equities fundamentals, intraday candles, news, …):
rate limits, timeouts, blocks, parse errors, HTTP faults. Domain-specific
codes (e.g. ``NO_PRICE_DATA`` for the equity pipeline, ``NO_INTRADAY_DATA``
for the intraday pipeline) live in the respective module packages and extend
this taxonomy.

The classifiers are deliberately conservative: anything that cannot be
confidently bucketed becomes ``PROVIDER_UNKNOWN_ERROR`` (a provider-side
bucket), never a symbol problem and never an assertion like "delisted".

This module is pure (no third-party imports) so it is trivially testable.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Taxonomy (string constants -- JSON-friendly, stable across versions)
# ---------------------------------------------------------------------------

OK = "OK"

# Honest "the call worked but there is no data" outcome (domain packages add
# more specific variants).
PROVIDER_EMPTY_RESPONSE = "PROVIDER_EMPTY_RESPONSE"

# Provider-side / transport failures (systemic when they hit many symbols).
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_BLOCKED = "PROVIDER_BLOCKED"
PROVIDER_JSON_PARSE_ERROR = "PROVIDER_JSON_PARSE_ERROR"
PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
PROVIDER_UNKNOWN_ERROR = "PROVIDER_UNKNOWN_ERROR"

# Error types that indicate the *provider* (not the symbol) is at fault. A
# high rate of these across benchmark symbols is the signature of a systemic
# outage rather than a per-symbol miss.
PROVIDER_SIDE_ERRORS = frozenset({
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    PROVIDER_BLOCKED,
    PROVIDER_JSON_PARSE_ERROR,
    PROVIDER_HTTP_ERROR,
    PROVIDER_UNKNOWN_ERROR,
})


# ---------------------------------------------------------------------------
# Classifiers
# ---------------------------------------------------------------------------

# Substrings we look for in an exception's string form. Ordered by specificity.
_RATE_LIMIT_HINTS = ("429", "too many requests", "rate limit", "rate-limit", "ratelimited")
_TIMEOUT_HINTS = ("timed out", "timeout", "read timed out", "connection timed out")
_BLOCKED_HINTS = (
    "forbidden", "403", "401", "unauthorized", "access denied",
    "connection refused", "connection reset", "connection aborted",
    "name or service not known", "failed to resolve", "nameresolution",
    "max retries", "ssl", "certificate",
)
_JSON_HINTS = (
    "expecting value", "json", "jsondecode", "extra data",
    "unterminated string", "delimiter",
)
_HTTP_HINTS = ("http", "status code", "5xx", "500", "502", "503", "504", "bad gateway")


def classify_exception(exc: BaseException) -> str:
    """Map a raised exception to an error-type constant.

    The mapping is heuristic but deliberately conservative: anything we cannot
    confidently bucket becomes ``PROVIDER_UNKNOWN_ERROR`` (a provider-side
    bucket), never a symbol problem and never "delisted".
    """
    return _classify_blob(type(exc).__name__.lower(), str(exc).lower())


def classify_error_text(text: Optional[str]) -> str:
    """Classify a free-text error string (e.g. a record's ``error`` field).

    Used when we only have the stored message, not the live exception object
    (e.g. when re-reading a provider record during a health check).
    """
    if not text:
        return PROVIDER_UNKNOWN_ERROR
    low = text.lower()
    # A stored message is often "ExcType: message"; treat the whole thing as the
    # message body and let the same hints fire.
    return _classify_blob(low, low)


def _classify_blob(name: str, msg: str) -> str:
    blob = f"{name} {msg}"
    # JSON-parse first: Yahoo's block returns "Expecting value: line 1 ..." and
    # the *type* is often a JSONDecodeError / ValueError. This is the signature
    # of the systemic failure we saw live.
    if "jsondecode" in name or any(h in msg for h in _JSON_HINTS):
        return PROVIDER_JSON_PARSE_ERROR
    if any(h in blob for h in _RATE_LIMIT_HINTS):
        return PROVIDER_RATE_LIMITED
    if "timeout" in name or any(h in msg for h in _TIMEOUT_HINTS):
        return PROVIDER_TIMEOUT
    if any(h in blob for h in _BLOCKED_HINTS):
        return PROVIDER_BLOCKED
    if "httperror" in name or any(h in msg for h in _HTTP_HINTS):
        return PROVIDER_HTTP_ERROR
    return PROVIDER_UNKNOWN_ERROR


def is_provider_side(error_type: Optional[str]) -> bool:
    """True if ``error_type`` indicates a provider/transport fault (systemic-capable)."""
    return error_type in PROVIDER_SIDE_ERRORS
