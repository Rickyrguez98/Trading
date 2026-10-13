"""Configuration for the portfolio monitor (US Equities Milestone 3).

Loaded from ``configs/us_equities/monitoring.yaml`` (or ``MONITORING_CONFIG``),
merged over the dataclass defaults below, and validated before use. Every
threshold the decision engine applies lives here so a reader can see -- and
change -- exactly what turns evidence into an action.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

_DEFAULT_CONFIG_PATH = Path("configs/us_equities/monitoring.yaml")

VALID_CURRENCIES = ("MXN", "USD")
VALID_MARKETS = ("US", "SIC", "BMV")
VALID_BACKENDS = ("bootstrap", "chronos")


class MonitoringConfigError(ValueError):
    """Raised with every problem found, not just the first."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        super().__init__("invalid monitoring config:\n  - " + "\n  - ".join(self.problems))


@dataclass
class RunConfig:
    output_dir: str = "reports/us_equities/monitoring"
    processed_dir: str = "data/us_equities/processed/monitoring"
    # The currency every total is reported in. MXN is what a GBM client
    # actually experiences: an SIC position gains or loses with USDMXN too.
    reporting_currency: str = "MXN"
    # Daily history fetched per holding (trend, drawdown, forecasting context
    # and walk-forward validation all come from this one series).
    history_years: float = 5.0
    # Latest full selection ranking: the "is the thesis still intact?" check.
    selection_results_csv: Optional[str] = "data/us_equities/processed/asset_selection_results.csv"
    selection_summary_json: Optional[str] = "reports/us_equities/asset_selection_summary.json"
    # A quote older than this many calendar days is flagged as stale.
    max_price_age_days: int = 5
    # A thesis checked against an old ranking is flagged; --refresh-signals fixes it.
    max_selection_age_days: int = 30
    cache_dir: str = "data/us_equities/cache"
    cache_ttl_seconds: int = 43_200


@dataclass
class TrendConfig:
    sma_short: int = 50
    sma_long: int = 200
    volatility_window: int = 63
    # Current volatility is compared with its own median over this window.
    volatility_regime_lookback: int = 252
    # Ratio above which volatility counts as an elevated regime.
    volatility_regime_alert: float = 1.5


@dataclass
class EventsConfig:
    # No ADD in the N calendar days before a scheduled earnings report: the
    # outcome is a coin flip with a fat tail, so waiting costs little.
    earnings_blackout_days: int = 7
    surprise_history: int = 4


@dataclass
class ForecastConfig:
    backend: str = "bootstrap"            # bootstrap | chronos
    chronos_model: str = "amazon/chronos-2"
    horizons: List[int] = field(default_factory=lambda: [21, 63, 126])   # trading days
    context_days: int = 512
    quantile_levels: List[float] = field(
        default_factory=lambda: [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    )
    # Price moves the report asks "how likely, and how soon?" about.
    gain_targets_pct: List[float] = field(default_factory=lambda: [5.0, 10.0, 20.0])
    # Bootstrap backend.
    n_paths: int = 2000
    block_length: int = 10                 # mean block length of the stationary bootstrap
    bootstrap_window_days: int = 756       # ~3 years of daily returns resampled
    # "zero" = no directional view (volatility only). A float is an annual drift.
    bootstrap_drift: Union[str, float] = "zero"
    seed: int = 7
    # Walk-forward validation: is the model better than a random walk?
    validate: bool = True
    validation_origins: int = 24
    validation_step: int = 21
    validation_alpha: float = 0.05
    # Forecast windows overlap when the horizon exceeds the step, so 24 windows
    # at a 126-day horizon hold only ~4 independent observations. A horizon
    # with fewer effective windows than this can never be called informative.
    min_effective_windows: float = 8.0
    min_history_days: int = 300


@dataclass
class DecisionConfig:
    commission_pct: float = 0.002          # per order, same default as allocation
    allow_fractional_shares: bool = False
    min_trade_value: float = 500.0         # in the reporting currency
    # Relative drift band around a target weight: trim above target*(1+band),
    # add below target*(1-band).
    rebalance_band: float = 0.25
    # Concentration cap applied when no target portfolio is given.
    max_weight_per_name: float = 0.15
    # Thesis thresholds on the selection fundamentals score (0-100).
    fundamentals_weak: float = 50.0
    fundamentals_broken: float = 40.0
    # Effective (confidence-adjusted) sentiment bands (0-100, 50 = neutral).
    sentiment_positive: float = 60.0
    sentiment_negative: float = 40.0
    min_sentiment_confidence: float = 0.3
    # Composite evidence score needed before an ADD is proposed.
    add_threshold: float = 1.5
    # Weakened thesis + negative composite: reduce by this fraction.
    trim_fraction_on_weakness: float = 0.5
    # Drawdown from the post-purchase peak that forces a REVIEW flag.
    drawdown_review_pct: float = 25.0
    # Proceeds of SELL/TRIM may fund ADDs (a rebalance), plus any new cash.
    reinvest_proceeds: bool = True


@dataclass
class MonitoringConfig:
    run: RunConfig = field(default_factory=RunConfig)
    trend: TrendConfig = field(default_factory=TrendConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_config(
    source: Union[str, Path, Dict[str, Any], None] = None,
    *,
    validate: bool = True,
) -> MonitoringConfig:
    """Load from a path, a dict, or the default on-disk config."""
    raw: Dict[str, Any]
    if isinstance(source, dict):
        raw = source
    else:
        path = _resolve_path(source)
        if isinstance(source, (str, Path)) and (path is None or not path.exists()):
            raise FileNotFoundError(f"Monitoring config not found: {source}")
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}

    cfg = _from_dict(raw)
    if validate:
        problems = validate_config(cfg)
        if problems:
            raise MonitoringConfigError(problems)
    return cfg


def _resolve_path(source: Union[str, Path, None]) -> Optional[Path]:
    if isinstance(source, (str, Path)):
        return Path(source)
    env = os.environ.get("MONITORING_CONFIG")
    if env:
        return Path(env)
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH
    return None


def _from_dict(raw: Dict[str, Any]) -> MonitoringConfig:
    def section(name: str) -> Dict[str, Any]:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    return MonitoringConfig(
        run=RunConfig(**_filtered(RunConfig, section("run"))),
        trend=TrendConfig(**_filtered(TrendConfig, section("trend"))),
        events=EventsConfig(**_filtered(EventsConfig, section("events"))),
        forecast=ForecastConfig(**_filtered(ForecastConfig, section("forecast"))),
        decision=DecisionConfig(**_filtered(DecisionConfig, section("decision"))),
    )


def _filtered(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in valid}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_config(cfg: MonitoringConfig) -> List[str]:
    """Return every problem found (empty list = valid)."""
    p: List[str] = []
    r, t, f, d = cfg.run, cfg.trend, cfg.forecast, cfg.decision

    if str(r.reporting_currency).upper() not in VALID_CURRENCIES:
        p.append(f"run.reporting_currency must be one of {VALID_CURRENCIES}")
    if r.history_years <= 0:
        p.append("run.history_years must be positive")

    if not (0 < t.sma_short < t.sma_long):
        p.append("trend.sma_short must be positive and smaller than trend.sma_long")
    if t.volatility_window < 5:
        p.append("trend.volatility_window must be at least 5 days")

    if f.backend not in VALID_BACKENDS:
        p.append(f"forecast.backend must be one of {VALID_BACKENDS}")
    if not f.horizons or any(int(h) <= 0 for h in f.horizons):
        p.append("forecast.horizons must be a non-empty list of positive trading-day counts")
    levels = list(f.quantile_levels)
    if not levels or any(not (0.0 < q < 1.0) for q in levels) or sorted(levels) != levels:
        p.append("forecast.quantile_levels must be sorted and strictly between 0 and 1")
    elif 0.5 not in levels:
        p.append("forecast.quantile_levels must include 0.5 (the median)")
    if f.n_paths < 200:
        p.append("forecast.n_paths must be at least 200 for stable probabilities")
    if f.block_length < 1:
        p.append("forecast.block_length must be at least 1")
    if not (f.bootstrap_drift == "zero" or isinstance(f.bootstrap_drift, (int, float))):
        p.append("forecast.bootstrap_drift must be 'zero' or an annual drift number")
    if f.validation_origins < 8:
        p.append("forecast.validation_origins must be at least 8 for a meaningful test")
    if not (0.0 < f.validation_alpha < 0.5):
        p.append("forecast.validation_alpha must be between 0 and 0.5")
    if f.min_effective_windows < 2:
        p.append("forecast.min_effective_windows must be at least 2")

    if not (0.0 <= d.commission_pct < 0.05):
        p.append("decision.commission_pct must be in [0, 0.05)")
    if not (0.0 < d.rebalance_band < 1.0):
        p.append("decision.rebalance_band must be in (0, 1)")
    if not (0.0 < d.max_weight_per_name <= 1.0):
        p.append("decision.max_weight_per_name must be in (0, 1]")
    if not (d.fundamentals_broken < d.fundamentals_weak):
        p.append("decision.fundamentals_broken must be below decision.fundamentals_weak")
    if not (d.sentiment_negative < 50.0 < d.sentiment_positive):
        p.append("decision.sentiment_negative < 50 < decision.sentiment_positive is required")
    if not (0.0 < d.trim_fraction_on_weakness <= 1.0):
        p.append("decision.trim_fraction_on_weakness must be in (0, 1]")
    return p
