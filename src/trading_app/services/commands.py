"""Build the exact pipeline commands the CLIs document, from validated inputs.

The app runs the same commands a person types (``trading-us-equities``,
``trading-portfolio``, ``trading-monitor``), as ``python -m`` invocations with
the current interpreter so they run in the app's environment. Every value is
validated here; the result is an argument list, never a shell string.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .paths import STRATEGIES, Paths

SELECTION_MODULE = "asset_selection.pipelines.run_asset_selection"
ALLOCATION_MODULE = "asset_selection.pipelines.run_portfolio_allocation"
MONITOR_MODULE = "asset_selection.pipelines.run_portfolio_monitor"
MAX_SAMPLE = 500
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,6}([.\-][A-Z])?$")


class InvalidRequest(ValueError):
    """User input that cannot become a command."""


def _py(module: str) -> List[str]:
    return [sys.executable, "-m", module]


def selection_command(paths: Paths, *, mode: str, limit: Optional[int] = None,
                      job_dir: Optional[Path] = None, tickers: Optional[List[str]] = None) -> List[str]:
    """Full run -> the canonical ranking. Sample run -> the job folder only.

    Fundamentals scores are relative to the set scored together, so a sample
    ranking must never replace the full-universe one every later step reads.
    """
    argv = _py(SELECTION_MODULE) + ["--config", str(paths.selection_config), "--top", "20"]
    if mode == "full":
        return argv
    if mode == "mine":
        # Only your stocks: ~20 price, fundamentals and news requests instead of ~4,800. Scores are
        # relative to this small set, so like a sample it never replaces the full ranking.
        clean = [t for t in (str(x).strip().upper() for x in (tickers or [])) if TICKER_RE.match(t)]
        if not clean:
            raise InvalidRequest("no stocks of yours to analyse: import your trade confirmations or orders first")
        if job_dir is None:
            raise InvalidRequest("a my-stocks run needs its own job folder")
        return argv + ["--tickers", *clean[:MAX_SAMPLE], "--no-provider-health-check",
                       "--output-dir", str(job_dir / "reports"), "--processed-dir", str(job_dir / "processed")]
    if mode != "sample":
        raise InvalidRequest("mode must be 'full', 'sample' or 'mine'")
    try:
        n = int(limit or 50)
    except (TypeError, ValueError):
        raise InvalidRequest("sample size must be a whole number") from None
    if not 5 <= n <= MAX_SAMPLE:
        raise InvalidRequest(f"sample size must be between 5 and {MAX_SAMPLE}")
    if job_dir is None:
        raise InvalidRequest("a sample run needs its own job folder")
    return argv + ["--universe", "sample", "--limit", str(n),
                   "--output-dir", str(job_dir / "reports"),
                   "--processed-dir", str(job_dir / "processed")]


def allocation_command(paths: Paths, *, amount: float, strategy: Optional[str] = None,
                       offline: bool = False,
                       instrument_prices: Optional[Dict[str, float]] = None) -> List[str]:
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        raise InvalidRequest("amount must be a number") from None
    if not (amt > 0 and amt < 1e10):
        raise InvalidRequest("amount must be positive")
    argv = _py(ALLOCATION_MODULE) + ["--config", str(paths.portfolio_config), "--amount", f"{amt:.2f}"]
    if strategy:
        if strategy not in STRATEGIES:
            raise InvalidRequest(f"strategy must be one of {', '.join(STRATEGIES)}")
        argv += ["--strategy", strategy]
    if offline:
        argv.append("--offline")
    try:
        prices = {str(k).strip().upper(): float(v) for k, v in (instrument_prices or {}).items()}
    except (TypeError, ValueError, AttributeError):
        raise InvalidRequest("instrument prices must be a mapping of ticker to price") from None
    if any(not k.replace(".", "").replace("-", "").isalnum() or v <= 0 for k, v in prices.items()):
        raise InvalidRequest("instrument prices need a ticker and a positive price")
    if prices:
        argv += ["--instrument-price", *[f"{k}={v}" for k, v in sorted(prices.items())]]
    return argv


def monitor_command(paths: Paths, *, option: str, output_dir: Path,
                    holdings: Optional[Path] = None, target_strategy: Optional[str] = None,
                    cash: float = 0.0, forecast: str = "bootstrap",
                    refresh_signals: bool = False) -> List[str]:
    if option not in ("A", "B", "C"):
        raise InvalidRequest("broker option must be A, B or C")
    if forecast not in ("bootstrap", "chronos"):
        raise InvalidRequest("forecast must be 'bootstrap' or 'chronos'")
    try:
        cash_v = float(cash or 0.0)
    except (TypeError, ValueError):
        raise InvalidRequest("cash must be a number") from None
    if cash_v < 0:
        raise InvalidRequest("cash cannot be negative")
    argv = _py(MONITOR_MODULE) + ["--config", str(paths.monitoring_config), "--broker", option,
                                  "--output-dir", str(output_dir), "--forecast", forecast]
    if option in ("A", "B") and holdings is not None:
        argv += ["--holdings", str(holdings)]
    if target_strategy:
        plan = paths.plan_csv(target_strategy)
        if target_strategy not in STRATEGIES or not plan.exists():
            raise InvalidRequest(f"no {target_strategy} plan found; run the allocation first")
        argv += ["--target", str(plan)]
    if cash_v:
        argv += ["--cash", f"{cash_v:.2f}"]
    if refresh_signals:
        argv.append("--refresh-signals")
    return argv
