"""Broker options: where the monitor reads your holdings from.

    A  statement_pdf  GBM (or DriveWealth) monthly statement PDF
    B  holdings_csv   a holdings CSV you keep up to date after each trade
    C  ibkr_flex      Interactive Brokers Flex Web Service (read-only, automatic)

Pick one in ``configs/us_equities/monitoring.yaml`` (``broker.option``) or per
run with ``trading-monitor --broker A|B|C``. Every option produces the same
lots, so everything downstream is identical. Background and trade-offs:
``docs/us_equities/broker-integration-options.md``.

None of the options places orders, and none logs in with a password:
A and B read files you provide; C uses IBKR's read-only Flex token, taken from
the environment and never stored.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .config import BrokerConfig
from .holdings import HoldingsError, aggregate_lots, load_holdings_csv, write_holdings_csv
from .models import Holding

TEMPLATE_PATH = Path("configs/us_equities/holdings_template.csv")


@dataclass(frozen=True)
class BrokerOption:
    code: str
    key: str
    label: str
    how: str


OPTIONS: Dict[str, BrokerOption] = {
    "A": BrokerOption("A", "statement_pdf", "GBM statement PDF (monthly)",
                      "download the month's estado de cuenta from the GBM app and point "
                      "broker.statement_pdf (or --holdings) at it"),
    "B": BrokerOption("B", "holdings_csv", "Holdings CSV you keep updated (recommended)",
                      "run `trading-monitor --init-holdings`, then add a row after each trade"),
    "C": BrokerOption("C", "ibkr_flex", "Interactive Brokers Flex Web Service (read-only)",
                      "in IBKR Client Portal > Performance & Reports > Flex Queries, generate a Flex "
                      "Web Service token and an Activity Flex Query (Open Positions, Lot level); "
                      "set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID in .env"),
}
_ALIASES = {
    "a": "A", "pdf": "A", "statement": "A", "statement_pdf": "A",
    "b": "B", "csv": "B", "holdings_csv": "B",
    "c": "C", "ibkr": "C", "ibkr_flex": "C", "flex": "C",
}


class BrokerConfigError(HoldingsError):
    """The chosen broker option cannot be used as configured."""


def resolve_option(value: Optional[str]) -> BrokerOption:
    code = _ALIASES.get(str(value or "").strip().lower())
    if code is None:
        raise BrokerConfigError(
            f"unknown broker option {value!r}; choose A (statement PDF), B (holdings CSV) or C (IBKR Flex)"
        )
    return OPTIONS[code]


def infer_option(path: Optional[str]) -> Optional[BrokerOption]:
    """A holdings path implies its option: .pdf -> A, anything else -> B."""
    if not path:
        return None
    return OPTIONS["A"] if str(path).lower().endswith(".pdf") else OPTIONS["B"]


@dataclass
class HoldingsLoad:
    holdings: List[Holding]
    option: BrokerOption
    source: str
    extraction: Optional[Dict] = None        # PDF / IBKR extraction report
    problems: List[str] = field(default_factory=list)


@dataclass
class Readiness:
    option: BrokerOption
    ready: bool
    detail: str


def readiness(cfg: BrokerConfig, option: Optional[BrokerOption] = None) -> List[Readiness]:
    """Whether each option could run right now (paths exist, env vars set)."""
    out = []
    for opt in OPTIONS.values():
        if opt.code == "A":
            p = cfg.statement_pdf
            ok = bool(p) and Path(p).exists()
            detail = f"statement: {p}" if p else "no statement_pdf configured"
        elif opt.code == "B":
            p = cfg.holdings_csv
            ok = bool(p) and Path(p).exists()
            detail = f"holdings file: {p}" + ("" if ok else " (missing -- run --init-holdings)")
        else:
            missing = [n for n in (cfg.ibkr_token_env, cfg.ibkr_query_id_env) if not os.environ.get(n)]
            ok = not missing
            detail = ("IBKR credentials found in the environment" if ok
                      else f"missing environment variable(s): {', '.join(missing)}")
        out.append(Readiness(opt, ok, detail))
    return out


def init_holdings(cfg: BrokerConfig) -> Path:
    """Create the personal holdings CSV from the template (never overwrites)."""
    dest = Path(cfg.holdings_csv)
    if dest.exists():
        raise BrokerConfigError(f"{dest} already exists; edit it instead of re-creating it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_PATH, dest)
    return dest


def load_holdings(
    option: BrokerOption,
    cfg: BrokerConfig,
    *,
    path: Optional[str],
    processed_dir: Path,
    us_universe: Optional[Set[str]] = None,
) -> HoldingsLoad:
    """Load holdings through the chosen option."""
    if option.code == "C":
        from .ibkr_flex import load_ibkr_lots

        lots, report = load_ibkr_lots(
            token_env=cfg.ibkr_token_env, query_id_env=cfg.ibkr_query_id_env,
            poll_seconds=cfg.ibkr_poll_seconds, max_wait_seconds=cfg.ibkr_max_wait_seconds,
        )
        review = write_holdings_csv(lots, processed_dir / "extracted_ibkr_flex.csv")
        report.review_csv = str(review)
        return HoldingsLoad(aggregate_lots(lots), option, "IBKR Flex Web Service", report.to_dict())

    chosen = path or (cfg.statement_pdf if option.code == "A" else cfg.holdings_csv)
    if not chosen:
        raise BrokerConfigError(f"option {option.code} needs a file: pass --holdings or set it in the config")
    src = Path(chosen)
    is_pdf = src.suffix.lower() == ".pdf"
    if option.code == "A" and not is_pdf:
        raise BrokerConfigError(f"option A reads a statement PDF, but {src.name} is not a .pdf")
    if option.code == "B" and is_pdf:
        raise BrokerConfigError(f"option B reads a holdings CSV, but {src.name} is a PDF (use option A)")

    if option.code == "A":
        from .pdf_statement import extract_holdings_from_pdf

        lots, report = extract_holdings_from_pdf(src, us_universe=us_universe)
        review = write_holdings_csv(lots, processed_dir / f"extracted_{src.stem}.csv")
        report.review_csv = str(review)
        return HoldingsLoad(aggregate_lots(lots), option, str(src), report.to_dict())

    if not src.exists():
        hint = (" -- run `trading-monitor --init-holdings` to create it from the template"
                if path is None else "")
        raise BrokerConfigError(f"holdings file not found: {src}{hint}")
    lots, csv_report = load_holdings_csv(src)
    holdings = aggregate_lots(lots)
    for h in holdings:
        sources = {lot.source for lot in h.lots}
        for msg in csv_report.warnings:
            where, _, text = msg.partition(": ")
            if where in sources:
                h.warnings.append(text)
    return HoldingsLoad(holdings, option, str(src), None, [f"skipped {m}" for m in csv_report.skipped])
