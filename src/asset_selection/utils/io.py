"""Backward-compatible shim: filesystem helpers now live in ``trading_common.io``."""
from trading_common.io import (  # noqa: F401
    _json_default,
    ensure_dir,
    read_json,
    write_csv,
    write_json,
)
