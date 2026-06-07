"""Machine-readable provider error taxonomy + classifiers.

The pipeline must be able to tell *why* a provider returned no data, because
the response to each cause is different:

* a JSON-parse error or HTTP 429 means the **provider** is down/blocked
  (systemic) -- retry later, fall back, or refuse to rank;
* a genuinely unsupported/odd symbol is a **ticker** problem -- skip that one;
* a successful-but-empty payload is honest "no data" -- penalize, don't guess.

We deliberately **never** assert "delisted" from an empty or parse-error
response: delisting needs corroborating evidence, and calling AAPL "delisted"
because Yahoo rate-limited us would be a lie. ``POSSIBLY_DELISTED`` exists only
for callers that have such evidence; the classifiers here never emit it.

This module is pure (no third-party imports) so it is trivially testable.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Taxonomy (string constants -- JSON-friendly, stable across versions)
# ---------------------------------------------------------------------------

OK = "OK"

# Ticker-side problems (the symbol, not the provider).
INVALID_TICKER = "INVALID_TICKER"
UNSUPPORTED_PROVIDER_SYMBOL = "UNSUPPORTED_PROVIDER_SYMBOL"

# Honest "the call worked but there is no data" outcomes.
NO_PRICE_DATA = "NO_PRICE_DATA"
NO_FUNDAMENTAL_DATA = "NO_FUNDAMENTAL_DATA"
NO_NEWS_DATA = "NO_NEWS_DATA"
PROVIDER_EMPTY_RESPONSE = "PROVIDER_EMPTY_RESPONSE"

# --- Price-coverage taxonomy (audit fix) -----------------------------------
# These refine a bare NO_PRICE_DATA once we have *context* about the ticker.
# They never assert delisting; they say WHERE the gap is.
#
#   PRICE_ENDPOINT_NO_DATA        the price endpoint specifically returned
#                                 nothing (a price-feed miss, not a verdict on
#                                 the company).
#   PROVIDER_SYMBOL_RESOLUTION_FAILED  every symbol variant we tried was empty
#                                 -- the spelling, not the company, is suspect.
#   PRICE_PROVIDER_GAP            price failed but fundamentals/news for the SAME
#                                 canonical ticker succeeded -> the company is
#                                 clearly real and covered; this is a price-
#                                 provider coverage gap, NOT delisting.
#   PROVIDER_COVERAGE_GAP         the free provider simply does not cover this
#                                 name (no corroborating evidence either way).
#   CRITICAL_TICKER_PRICE_FAILURE a configured/important (mega-cap, benchmark,
#                                 watchlist) ticker lost its price -> material.
PRICE_ENDPOINT_NO_DATA = "PRICE_ENDPOINT_NO_DATA"
PROVIDER_SYMBOL_RESOLUTION_FAILED = "PROVIDER_SYMBOL_RESOLUTION_FAILED"
PRICE_PROVIDER_GAP = "PRICE_PROVIDER_GAP"
PROVIDER_COVERAGE_GAP = "PROVIDER_COVERAGE_GAP"
CRITICAL_TICKER_PRICE_FAILURE = "CRITICAL_TICKER_PRICE_FAILURE"

# Requires corroborating evidence; classifiers never emit this from a bare
# empty/parse response.
POSSIBLY_DELISTED = "POSSIBLY_DELISTED"

# Provider-side / transport failures (systemic when they hit many tickers).
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_BLOCKED = "PROVIDER_BLOCKED"
PROVIDER_JSON_PARSE_ERROR = "PROVIDER_JSON_PARSE_ERROR"
PROVIDER_HTTP_ERROR = "PROVIDER_HTTP_ERROR"
PROVIDER_UNKNOWN_ERROR = "PROVIDER_UNKNOWN_ERROR"

# Error types that indicate the *provider* (not the ticker) is at fault. A high
# rate of these across benchmark mega-caps is the signature of a systemic
# outage rather than a per-ticker miss.
PROVIDER_SIDE_ERRORS = frozenset({
    PROVIDER_RATE_LIMITED,
    PROVIDER_TIMEOUT,
    PROVIDER_BLOCKED,
    PROVIDER_JSON_PARSE_ERROR,
    PROVIDER_HTTP_ERROR,
    PROVIDER_UNKNOWN_ERROR,
})

# Refinements of a bare price miss. These are coverage/feed gaps, NOT transport
# outages and NOT delisting -- they classify *where* a price gap sits once we
# know whether other data for the same ticker succeeded.
PRICE_COVERAGE_REFINEMENTS = frozenset({
    PRICE_ENDPOINT_NO_DATA,
    PROVIDER_SYMBOL_RESOLUTION_FAILED,
    PRICE_PROVIDER_GAP,
    PROVIDER_COVERAGE_GAP,
    CRITICAL_TICKER_PRICE_FAILURE,
})

# "No data" outcomes that are not, on their own, evidence of a provider outage.
EMPTY_DATA_ERRORS = frozenset({
    NO_PRICE_DATA,
    NO_FUNDAMENTAL_DATA,
    NO_NEWS_DATA,
    PROVIDER_EMPTY_RESPONSE,
}) | PRICE_COVERAGE_REFINEMENTS


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
    bucket), never a ticker problem and never "delisted".
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


def classify_empty(data_type: str, *, remapped: bool = False) -> str:
    """Classify a successful-but-empty payload for ``data_type``.

    ``data_type`` is one of ``"price" | "fundamentals" | "news"``. We return the
    data-type-specific "no data" code. ``remapped`` (the canonical symbol was
    translated to a provider symbol) does not change the code -- we still do not
    assert delisting -- but callers may use it for messaging.
    """
    dt = (data_type or "").lower()
    if dt.startswith("price"):
        return NO_PRICE_DATA
    if dt.startswith("fund"):
        return NO_FUNDAMENTAL_DATA
    if dt.startswith("news"):
        return NO_NEWS_DATA
    return PROVIDER_EMPTY_RESPONSE


def is_provider_side(error_type: Optional[str]) -> bool:
    """True if ``error_type`` indicates a provider/transport fault (systemic-capable)."""
    return error_type in PROVIDER_SIDE_ERRORS


def status_for(error_type: Optional[str]) -> str:
    """Collapse an error type to the legacy coarse ``status`` field.

    Kept so existing consumers that only read ``status`` ("ok"/"empty"/"error")
    keep working while new consumers read the richer ``error_type``.
    """
    if error_type in (None, OK):
        return "ok"
    if error_type in EMPTY_DATA_ERRORS:
        return "empty"
    return "error"


def reclassify_price_failure(
    original_error_type: Optional[str],
    *,
    has_other_data: bool = False,
    all_variants_empty: bool = False,
) -> str:
    """Refine a bare price failure once we have context about the ticker.

    The point is to stop a price miss reading as "possibly delisted" for a name
    that demonstrably still trades. We only refine an *empty* outcome (the call
    succeeded but the price endpoint returned nothing): a transport/provider-side
    fault (JSON-parse, rate-limit, blocked, timeout, HTTP) is returned unchanged,
    because in a real outage that genuinely IS the cause and it must keep
    counting toward the systemic-failure budget.

    * ``has_other_data`` -- fundamentals or news for the SAME canonical ticker
      succeeded. The company is clearly real and covered, so the price miss is a
      ``PRICE_PROVIDER_GAP`` (a price-feed coverage gap), never delisting.
    * ``all_variants_empty`` -- every symbol variant we tried came back empty and
      nothing else corroborates the name -> ``PROVIDER_COVERAGE_GAP`` (the free
      provider just does not carry it).
    * otherwise -> ``PRICE_ENDPOINT_NO_DATA`` (an honest price-endpoint miss).
    """
    if is_provider_side(original_error_type):
        return original_error_type  # a real outage -- do not soften it
    if has_other_data:
        return PRICE_PROVIDER_GAP
    if all_variants_empty:
        return PROVIDER_COVERAGE_GAP
    return PRICE_ENDPOINT_NO_DATA
