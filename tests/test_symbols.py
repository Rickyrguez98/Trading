"""Per-provider ticker symbol normalization."""
from __future__ import annotations

from asset_selection.data_providers.symbols import (
    is_class_share,
    likely_no_data_reason,
    to_provider_symbol,
    was_remapped,
)


def test_yfinance_maps_dot_class_shares_to_hyphen():
    # The exact symbols that silently failed in the real full-universe run.
    assert to_provider_symbol("BRK.B", "yfinance") == "BRK-B"
    assert to_provider_symbol("BRK.A", "yfinance") == "BRK-A"
    assert to_provider_symbol("BF.B", "yfinance") == "BF-B"
    assert to_provider_symbol("AKO.B", "yfinance") == "AKO-B"
    assert to_provider_symbol("WSO.B", "yfinance") == "WSO-B"


def test_yfinance_leaves_plain_symbols_unchanged():
    assert to_provider_symbol("AAPL", "yfinance") == "AAPL"
    assert to_provider_symbol("nvda", "yfinance") == "NVDA"


def test_unknown_provider_is_a_noop():
    # We only know yfinance's convention; don't corrupt others.
    assert to_provider_symbol("BRK.B", "finnhub") == "BRK.B"


def test_was_remapped_flags_only_changed_symbols():
    assert was_remapped("BRK.B", "yfinance") is True
    assert was_remapped("AAPL", "yfinance") is False


def test_is_class_share_detects_dotted_class_shares():
    assert is_class_share("BRK.B")
    assert is_class_share("BF.A")
    assert not is_class_share("AAPL")


def test_no_data_reason_is_honest_not_a_delisted_claim():
    # After a remap, the message must not assert "delisted" as fact.
    msg = likely_no_data_reason("BRK.B", "BRK-B")
    assert "BRK.B->BRK-B" in msg
    assert "may" in msg  # hedged, not asserted
    # For a non-remapped symbol the message still hedges.
    msg2 = likely_no_data_reason("AAPL", "AAPL")
    assert "may" in msg2
