"""Configuration loading.

Configuration is YAML-first. We accept a path or a pre-loaded dict and return
a frozen-ish ``AppConfig`` dataclass tree so the rest of the code never has to
deal with raw dict spelunking.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

_DEFAULT_CONFIG_PATH = Path("configs/default_config.yaml")


# ---------------------------------------------------------------------------
# Dataclass tree
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    max_tickers: int = 500
    top_n: int = 25
    output_dir: str = "reports"
    processed_dir: str = "data/processed"


@dataclass
class UniverseConfig:
    sources: List[str] = field(default_factory=lambda: ["nasdaq_trader", "sec_company_tickers"])
    exclude_etfs: bool = True
    exclude_warrants: bool = True
    exclude_units: bool = True
    exclude_preferred: bool = True
    exclude_rights: bool = True
    exclude_test_issues: bool = True
    min_ticker_length: int = 1
    max_ticker_length: int = 5


@dataclass
class ProvidersConfig:
    fundamentals: str = "yfinance"
    prices: str = "yfinance"
    news: str = "yfinance"


@dataclass
class CacheConfig:
    enabled: bool = True
    dir: str = "data/cache"
    ttl_seconds: Dict[str, int] = field(
        default_factory=lambda: {
            "fundamentals": 86400,
            "prices": 43200,
            "news": 3600,
            "universe": 604800,
        }
    )


@dataclass
class PricesConfig:
    lookback_days: int = 90
    min_avg_dollar_volume: float = 1_000_000.0
    min_market_cap: float = 100_000_000.0
    weak_return_threshold: float = -0.10
    momentum_penalty_strength: float = 20.0


@dataclass
class SentimentConfig:
    model: str = "vader"
    max_age_days: int = 30
    recency_halflife_days: float = 7.0
    min_articles_for_confidence: int = 3
    low_confidence_threshold: float = 0.3


@dataclass
class ScoringConfig:
    winsor_lower_pct: float = 0.02
    winsor_upper_pct: float = 0.98
    missing_penalty_per_field: float = 0.05
    growth: Dict[str, float] = field(default_factory=dict)
    quality: Dict[str, float] = field(default_factory=dict)
    valuation: Dict[str, float] = field(default_factory=dict)
    balance_sheet: Dict[str, float] = field(default_factory=dict)
    cash_flow: Dict[str, float] = field(default_factory=dict)
    pillars: Dict[str, float] = field(default_factory=dict)


@dataclass
class CompositeConfig:
    weights: Dict[str, float] = field(default_factory=dict)
    speculative_hype: Dict[str, float] = field(default_factory=dict)
    strong_fundamentals_bad_sentiment: Dict[str, float] = field(default_factory=dict)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_to_file: bool = False
    file: str = "logs/asset_selection.log"


@dataclass
class AppConfig:
    run: RunConfig = field(default_factory=RunConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limits: Dict[str, float] = field(default_factory=lambda: {"yfinance": 0.4})
    prices: PricesConfig = field(default_factory=PricesConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    composite: CompositeConfig = field(default_factory=CompositeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def rate_limit_for(self) -> Dict[str, float]:
        return self.rate_limits


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_config(source: Union[str, Path, Dict[str, Any], None] = None) -> AppConfig:
    """Load a config from path, dict, or default-on-disk fallback.

    Resolution order:
        1. explicit ``source`` argument (path or dict)
        2. ``ASSET_SELECTION_CONFIG`` env var (path)
        3. ``configs/default_config.yaml``
        4. dataclass defaults (no file required)
    """
    raw: Dict[str, Any]
    if isinstance(source, dict):
        raw = source
    else:
        path = _resolve_path(source)
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}

    return _from_dict(raw)


def _resolve_path(source: Union[str, Path, None]) -> Optional[Path]:
    if isinstance(source, (str, Path)):
        return Path(source)
    env = os.environ.get("ASSET_SELECTION_CONFIG")
    if env:
        return Path(env)
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH
    return None


def _from_dict(raw: Dict[str, Any]) -> AppConfig:
    def section(name: str) -> Dict[str, Any]:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    return AppConfig(
        run=RunConfig(**_filtered(RunConfig, section("run"))),
        universe=UniverseConfig(**_filtered(UniverseConfig, section("universe"))),
        providers=ProvidersConfig(**_filtered(ProvidersConfig, section("providers"))),
        cache=CacheConfig(**_filtered(CacheConfig, section("cache"))),
        rate_limits=dict(section("rate_limits") or {"yfinance": 0.4}),
        prices=PricesConfig(**_filtered(PricesConfig, section("prices"))),
        sentiment=SentimentConfig(**_filtered(SentimentConfig, section("sentiment"))),
        scoring=ScoringConfig(**_filtered(ScoringConfig, section("scoring"))),
        composite=CompositeConfig(**_filtered(CompositeConfig, section("composite"))),
        logging=LoggingConfig(**_filtered(LoggingConfig, section("logging"))),
    )


def _filtered(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    # Tolerate unknown keys in the YAML so adding a field doesn't break older configs.
    valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in valid}
