"""Centralized logging configuration."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False


def configure_logging(
    level: Optional[str] = None,
    log_to_file: bool = False,
    file: str = "logs/asset_selection.log",
) -> None:
    """Idempotent root-logger setup. Call once at program start."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level_name = (
        level
        or os.environ.get("ASSET_SELECTION_LOG_LEVEL")
        or "INFO"
    ).upper()
    resolved_level = getattr(logging, resolved_level_name, logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_to_file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file))

    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(level=resolved_level, format=fmt, handlers=handlers)

    # Tame noisy third-party loggers.
    for noisy in ("urllib3", "yfinance", "peewee", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
