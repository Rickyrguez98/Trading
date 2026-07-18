"""Typed configuration for the binary-options selection module.

YAML-first, mirroring ``asset_selection.config``: a dataclass tree, a loader
that tolerates unknown keys (so adding a field never breaks older configs),
plus — new for this module — **strict semantic validation**. Impossible or
contradictory settings (unsupported intervals, horizons outside 5–120 minutes,
weight structures that let one family dominate, payout ratios that make no
sense, session contradictions) are rejected up front with every problem
listed, instead of surfacing as confusing behavior mid-run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

_DEFAULT_CONFIG_PATH = Path("configs/binary_options/default.yaml")

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

HORIZON_MIN_MINUTES = 5
HORIZON_MAX_MINUTES = 120
SHORT_GROUP_MAX_MINUTES = 30  # 5-30 inclusive = "short"

HORIZON_GROUP_SHORT = "short"
HORIZON_GROUP_EXTENDED = "extended_intraday"

# Provider-native intraday intervals we accept. Coarse data is never resampled
# into a finer interval, so the interval must be natively supported.
SUPPORTED_INTERVALS: Dict[str, int] = {
    "1m": 1,
    "2m": 2,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1h": 60,
}

# The directional feature families the scoring layer understands. Weight dicts
# may only use these keys.
FEATURE_FAMILIES = (
    "momentum",
    "trend",
    "mean_reversion",
    "volume",
    "context",
    "sentiment",
)

UNIVERSE_MODES = ("custom", "watchlist", "asset-selection")
SESSION_MODES = ("regular", "extended")


def horizon_group(minutes: int) -> str:
    """Classify a horizon in minutes into its configuration group."""
    return HORIZON_GROUP_SHORT if minutes <= SHORT_GROUP_MAX_MINUTES else HORIZON_GROUP_EXTENDED


class ConfigValidationError(ValueError):
    """Raised when the configuration is semantically invalid.

    Carries *all* detected problems so a user can fix them in one pass.
    """

    def __init__(self, problems: List[str]) -> None:
        self.problems = list(problems)
        joined = "\n  - ".join(self.problems)
        super().__init__(f"Invalid binary-options configuration:\n  - {joined}")


# ---------------------------------------------------------------------------
# Dataclass tree
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    top_n: int = 20
    output_dir: str = "reports/binary_options"
    processed_dir: str = "data/binary_options/processed"
    input_dir: str = "data/binary_options/input"

    def __post_init__(self) -> None:
        # Migrate flat legacy paths onto the domain-separated layout so the
        # two research products can never write into each other's outputs.
        if self.output_dir == "reports":
            self.output_dir = "reports/binary_options"
        if self.processed_dir == "data/processed":
            self.processed_dir = "data/binary_options/processed"


@dataclass
class UniverseConfig:
    # "custom" (explicit tickers), "watchlist" (configured list / file), or
    # "asset-selection" (consume the equity pipeline's output CSV as an
    # upstream universe/quality filter -- never as a direct intraday predictor).
    mode: str = "watchlist"
    tickers: List[str] = field(default_factory=list)
    watchlist: List[str] = field(default_factory=lambda: ["AAPL", "MSFT", "NVDA", "SPY"])
    watchlist_file: Optional[str] = None
    # Path to data/processed/asset_selection_results.csv (or compatible).
    asset_selection_input: str = "data/us_equities/processed/asset_selection_results.csv"
    # When consuming the asset-selection output, keep at most this many of the
    # top-ranked names (0/None disables the cap).
    asset_selection_top_k: Optional[int] = 25
    # Hard cap for any mode -- intraday fetches are per-symbol and rate-limited.
    max_symbols: int = 50
    # Benchmark used for market-context features (fetched alongside the universe).
    benchmark_symbol: str = "SPY"


@dataclass
class HorizonsConfig:
    minutes: List[int] = field(default_factory=lambda: [5, 10, 15, 30, 60, 90, 120])


@dataclass
class CandlesConfig:
    interval: str = "5m"
    # Bars of history requested/required before features are trusted.
    required_history_bars: int = 60
    # Extra bars requested beyond the requirement (headroom for gaps).
    fetch_history_bars: int = 120
    # A signal is stale when (now - latest completed candle close) exceeds
    # staleness_intervals * interval.
    max_staleness_intervals: float = 3.0
    # An in-progress candle (start + interval > now) is dropped unless this is
    # explicitly enabled. Enabling it is honest only for research replays.
    allow_incomplete_last_candle: bool = False

    @property
    def interval_minutes(self) -> int:
        return SUPPORTED_INTERVALS.get(self.interval, 0)

    @property
    def interval_seconds(self) -> int:
        return self.interval_minutes * 60


@dataclass
class MarketSessionConfig:
    timezone: str = "America/New_York"
    regular_open: str = "09:30"
    regular_close: str = "16:00"
    premarket_open: str = "04:00"
    afterhours_close: str = "20:00"
    # Which candles/sessions the analysis may use.
    session: str = "regular"  # "regular" | "extended"
    allow_premarket: bool = False
    allow_afterhours: bool = False
    # When true, a signal generated while the market is not in an allowed
    # session becomes NO_TRADE (MARKET_CLOSED) instead of a live signal.
    require_open_market: bool = True


@dataclass
class ProvidersConfig:
    intraday: str = "yfinance"
    # Ordered fallback chain; empty means use `intraday` alone. Only providers
    # that are genuinely implemented may appear here (validated).
    priority: List[str] = field(default_factory=list)


@dataclass
class CacheConfig:
    enabled: bool = True
    dir: str = "data/binary_options/cache"
    # Interval-aware TTL: ttl = clamp(interval_seconds * ttl_interval_fraction,
    # min_ttl_seconds, max_ttl_seconds). Intraday data must never inherit the
    # multi-day TTLs of the fundamentals pipeline.
    ttl_interval_fraction: float = 0.5
    min_ttl_seconds: int = 15
    max_ttl_seconds: int = 900

    def ttl_for_interval(self, interval_seconds: int) -> int:
        ttl = int(interval_seconds * self.ttl_interval_fraction)
        return max(self.min_ttl_seconds, min(self.max_ttl_seconds, ttl))


@dataclass
class LiquidityConfig:
    min_avg_volume: float = 10_000.0            # shares per bar (rolling mean)
    min_avg_dollar_volume: float = 250_000.0    # close * volume per bar
    max_zero_volume_ratio: float = 0.20         # fraction of bars with volume == 0


@dataclass
class DataQualityConfig:
    max_missing_ratio: float = 0.10        # missing bars / expected bars
    max_duplicate_ratio: float = 0.02
    min_price: float = 1.0                 # sub-dollar names are excluded
    # A single-bar |return| above this is treated as abnormal (possible bad
    # print / corrupted feed) and flagged.
    max_abnormal_bar_return: float = 0.20
    # Rows below this data-quality score (0-100) cannot carry a live signal.
    min_data_quality_score: float = 60.0


@dataclass
class FeatureParamsConfig:
    """Windows are in BARS unless stated otherwise; horizon-scaled windows are
    computed as multiples of the horizon expressed in bars (see METHODOLOGY)."""
    # Momentum: returns over `horizon_bars * multiple` for each multiple.
    return_window_multiples: List[int] = field(default_factory=lambda: [1, 3, 6])
    ema_fast_bars: int = 9
    ema_slow_bars: int = 21
    # Trend
    macd_fast_bars: int = 12
    macd_slow_bars: int = 26
    macd_signal_bars: int = 9
    # Mean reversion / oscillators
    rsi_period_bars: int = 14
    bollinger_period_bars: int = 20
    bollinger_k: float = 2.0
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    # Volatility / candle structure
    atr_period_bars: int = 14
    range_window_bars: int = 20
    compression_range_ratio: float = 0.5   # current range < ratio * median = compression
    expansion_range_ratio: float = 2.0     # current range > ratio * median = expansion
    abnormal_range_multiple: float = 5.0   # current range > multiple * median = abnormal
    # Volume
    volume_window_bars: int = 20
    high_relative_volume: float = 1.5      # rel. volume above this confirms moves
    # Context
    context_return_window_multiples: List[int] = field(default_factory=lambda: [1, 3])


@dataclass
class WeightsConfig:
    """Feature-family weights per horizon group.

    Values are relative weights over ``FEATURE_FAMILIES``; the scoring layer
    renormalizes over the families actually available for a given row, so the
    absolute scale does not matter -- the *ratios* do. Validation prevents a
    single family from dominating and caps sentiment (which, for short
    horizons, must not override live price/volume evidence).
    """
    short: Dict[str, float] = field(default_factory=lambda: {
        "momentum": 0.30,
        "trend": 0.20,
        "mean_reversion": 0.15,
        "volume": 0.20,
        "context": 0.15,
        "sentiment": 0.00,
    })
    extended_intraday: Dict[str, float] = field(default_factory=lambda: {
        "momentum": 0.20,
        "trend": 0.30,
        "mean_reversion": 0.10,
        "volume": 0.15,
        "context": 0.20,
        "sentiment": 0.05,
    })

    def for_group(self, group: str) -> Dict[str, float]:
        return dict(self.short if group == HORIZON_GROUP_SHORT else self.extended_intraday)


@dataclass
class SignalRulesConfig:
    # |directional_score| (0-100 points) below this -> NO_TRADE (scores too close).
    min_directional_margin: float = 15.0
    # confidence_score (0-1) below this -> NO_TRADE.
    min_confidence: float = 0.25
    # min(call,put)/max(call,put) above this -> NO_TRADE (contradictory evidence).
    max_contradiction_ratio: float = 0.60
    # Multiplier on the contradiction ratio inside the confidence formula.
    contradiction_penalty_weight: float = 0.50
    # Strength bands.
    strong_min_directional: float = 40.0
    strong_min_confidence: float = 0.50
    moderate_min_directional: float = 25.0
    # Volatility-regime factor: confidence is multiplied by a factor in
    # [vol_regime_floor, 1]; abnormal ranges push it toward the floor.
    vol_regime_floor: float = 0.50


@dataclass
class SentimentConfig:
    """Optional, strictly secondary sentiment input.

    The only sentiment source in this milestone is the equity pipeline's
    output CSV (``sentiment_score`` 0-100), available when the universe mode is
    ``asset-selection``. It applies only to horizons at/above
    ``min_horizon_minutes`` and its weight is capped by validation, so broad,
    possibly stale news tone can never override live price/volume evidence.
    """
    enabled: bool = False
    min_horizon_minutes: int = 60
    # Sentiment scores further than this from neutral (50) are clamped before
    # mapping to [-1, 1], keeping the family bounded.
    max_abs_tilt: float = 25.0


@dataclass
class PayoutConfig:
    """Optional binary-contract payout assumption (no broker integration).

    ``payout_ratio`` r is the net win per unit staked (e.g. 0.85 means a
    winning contract returns the stake plus 85%). When configured, the
    break-even win probability p* = 1 / (1 + r) is reported as CONTEXT ONLY:
    this module produces heuristic confidence scores, not calibrated
    probabilities, so no expected value / edge is ever computed from them.
    """
    payout_ratio: Optional[float] = None


@dataclass
class RobustnessConfig:
    allow_partial_results: bool = True
    # Provider-side failure fraction (across attempted symbols) at/above which
    # the run is INVALID rather than DEGRADED.
    max_provider_failure_ratio: float = 0.50
    # Benchmark symbols probed by --health-check-only and the pre-run probe.
    health_check_symbols: List[str] = field(default_factory=lambda: ["SPY", "AAPL"])
    run_health_check: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_to_file: bool = False
    file: str = "logs/binary_options_selection.log"


@dataclass
class ValidationConfig:
    enabled: bool = True


@dataclass
class BinaryOptionsConfig:
    run: RunConfig = field(default_factory=RunConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    horizons: HorizonsConfig = field(default_factory=HorizonsConfig)
    candles: CandlesConfig = field(default_factory=CandlesConfig)
    market_session: MarketSessionConfig = field(default_factory=MarketSessionConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limits: Dict[str, float] = field(default_factory=lambda: {"yfinance": 0.4})
    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    data_quality: DataQualityConfig = field(default_factory=DataQualityConfig)
    features: FeatureParamsConfig = field(default_factory=FeatureParamsConfig)
    weights: WeightsConfig = field(default_factory=WeightsConfig)
    signal_rules: SignalRulesConfig = field(default_factory=SignalRulesConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    payout: PayoutConfig = field(default_factory=PayoutConfig)
    robustness: RobustnessConfig = field(default_factory=RobustnessConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(
    source: Union[str, Path, Dict[str, Any], None] = None,
    *,
    validate: bool = True,
) -> BinaryOptionsConfig:
    """Load a config from path, dict, or default-on-disk fallback.

    Resolution order:
        1. explicit ``source`` argument (path or dict)
        2. ``BINARY_OPTIONS_CONFIG`` env var (path)
        3. ``configs/binary_options/default.yaml``
        4. dataclass defaults (no file required)

    Raises :class:`ConfigValidationError` (listing every problem) unless
    ``validate=False``.
    """
    raw: Dict[str, Any]
    if isinstance(source, dict):
        raw = source
    else:
        path = _resolve_path(source)
        if isinstance(source, (str, Path)) and (path is None or not path.exists()):
            raise FileNotFoundError(f"Config file not found: {source}")
        if path is not None and path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = {}

    cfg = _from_dict(raw)
    if validate:
        problems = validate_config(cfg)
        if problems:
            raise ConfigValidationError(problems)
    return cfg


def _resolve_path(source: Union[str, Path, None]) -> Optional[Path]:
    if isinstance(source, (str, Path)):
        return Path(source)
    env = os.environ.get("BINARY_OPTIONS_CONFIG")
    if env:
        return Path(env)
    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH
    if _LEGACY_CONFIG_PATH.exists():
        import warnings

        warnings.warn(
            f"{_LEGACY_CONFIG_PATH} is deprecated; move it to "
            f"{_DEFAULT_CONFIG_PATH} (see docs/shared/migration.md)",
            DeprecationWarning,
            stacklevel=3,
        )
        return _LEGACY_CONFIG_PATH
    return None


def _from_dict(raw: Dict[str, Any]) -> BinaryOptionsConfig:
    def section(name: str) -> Dict[str, Any]:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    return BinaryOptionsConfig(
        run=RunConfig(**_filtered(RunConfig, section("run"))),
        universe=UniverseConfig(**_filtered(UniverseConfig, section("universe"))),
        horizons=HorizonsConfig(**_filtered(HorizonsConfig, section("horizons"))),
        candles=CandlesConfig(**_filtered(CandlesConfig, section("candles"))),
        market_session=MarketSessionConfig(
            **_filtered(MarketSessionConfig, section("market_session"))
        ),
        providers=ProvidersConfig(**_filtered(ProvidersConfig, section("providers"))),
        cache=CacheConfig(**_filtered(CacheConfig, section("cache"))),
        rate_limits=dict(section("rate_limits") or {"yfinance": 0.4}),
        liquidity=LiquidityConfig(**_filtered(LiquidityConfig, section("liquidity"))),
        data_quality=DataQualityConfig(**_filtered(DataQualityConfig, section("data_quality"))),
        features=FeatureParamsConfig(**_filtered(FeatureParamsConfig, section("features"))),
        weights=WeightsConfig(**_filtered(WeightsConfig, section("weights"))),
        signal_rules=SignalRulesConfig(**_filtered(SignalRulesConfig, section("signal_rules"))),
        sentiment=SentimentConfig(**_filtered(SentimentConfig, section("sentiment"))),
        payout=PayoutConfig(**_filtered(PayoutConfig, section("payout"))),
        robustness=RobustnessConfig(**_filtered(RobustnessConfig, section("robustness"))),
        logging=LoggingConfig(**_filtered(LoggingConfig, section("logging"))),
        validation=ValidationConfig(**_filtered(ValidationConfig, section("validation"))),
    )


def _filtered(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    # Tolerate unknown keys in the YAML so adding a field doesn't break older configs.
    valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in valid}


# ---------------------------------------------------------------------------
# Semantic validation
# ---------------------------------------------------------------------------

# Providers that genuinely exist in data_providers/__init__.py. Kept here so
# config validation can reject fallback chains naming unimplemented providers.
IMPLEMENTED_PROVIDERS = ("yfinance",)

# A single family may not carry more than this fraction of the total weight,
# so one (possibly redundant) indicator family cannot dominate by accident.
MAX_SINGLE_FAMILY_FRACTION = 0.60
# Sentiment caps (fractions of total weight) per horizon group.
MAX_SENTIMENT_FRACTION_SHORT = 0.05
MAX_SENTIMENT_FRACTION_EXTENDED = 0.15


def validate_config(cfg: BinaryOptionsConfig) -> List[str]:
    """Return a list of human-readable problems (empty = valid)."""
    problems: List[str] = []

    # --- universe ---
    if cfg.universe.mode not in UNIVERSE_MODES:
        problems.append(
            f"universe.mode '{cfg.universe.mode}' is not one of {UNIVERSE_MODES}"
        )
    if cfg.universe.max_symbols <= 0:
        problems.append("universe.max_symbols must be positive")

    # --- interval ---
    interval = cfg.candles.interval
    if interval not in SUPPORTED_INTERVALS:
        problems.append(
            f"candles.interval '{interval}' unsupported; choose one of "
            f"{sorted(SUPPORTED_INTERVALS)} (coarse data is never resampled finer)"
        )
    interval_minutes = SUPPORTED_INTERVALS.get(interval)

    # --- horizons ---
    if not cfg.horizons.minutes:
        problems.append("horizons.minutes must not be empty")
    for h in cfg.horizons.minutes:
        if not isinstance(h, int) or isinstance(h, bool):
            problems.append(f"horizon '{h}' must be an integer number of minutes")
            continue
        if h < HORIZON_MIN_MINUTES or h > HORIZON_MAX_MINUTES:
            problems.append(
                f"horizon {h}m outside the supported range "
                f"[{HORIZON_MIN_MINUTES}, {HORIZON_MAX_MINUTES}] minutes"
            )
        elif interval_minutes:
            if h < interval_minutes:
                problems.append(
                    f"horizon {h}m is shorter than the candle interval {interval}"
                )
            elif h % interval_minutes != 0:
                problems.append(
                    f"horizon {h}m is not a whole multiple of the candle interval "
                    f"{interval}; expiration would fall inside a candle"
                )
    if len(set(cfg.horizons.minutes)) != len(cfg.horizons.minutes):
        problems.append("horizons.minutes contains duplicates")

    # --- candles / history ---
    if cfg.candles.required_history_bars <= 0:
        problems.append("candles.required_history_bars must be positive")
    if cfg.candles.fetch_history_bars < cfg.candles.required_history_bars:
        problems.append(
            "candles.fetch_history_bars must be >= candles.required_history_bars"
        )
    if cfg.candles.max_staleness_intervals <= 0:
        problems.append("candles.max_staleness_intervals must be positive")

    # --- session ---
    ms = cfg.market_session
    if ms.session not in SESSION_MODES:
        problems.append(f"market_session.session '{ms.session}' not in {SESSION_MODES}")
    if ms.session == "regular" and (ms.allow_premarket or ms.allow_afterhours):
        problems.append(
            "market_session.session is 'regular' but allow_premarket/allow_afterhours "
            "is enabled -- enable session: extended to analyze extended hours"
        )
    for label, val in (
        ("regular_open", ms.regular_open),
        ("regular_close", ms.regular_close),
        ("premarket_open", ms.premarket_open),
        ("afterhours_close", ms.afterhours_close),
    ):
        if not _valid_hhmm(val):
            problems.append(f"market_session.{label} '{val}' is not HH:MM")

    # --- providers ---
    chain = cfg.providers.priority or [cfg.providers.intraday]
    for name in [cfg.providers.intraday, *cfg.providers.priority]:
        if name not in IMPLEMENTED_PROVIDERS:
            problems.append(
                f"provider '{name}' is not implemented (available: {IMPLEMENTED_PROVIDERS})"
            )
    if len(chain) != len(set(chain)):
        problems.append("providers.priority contains duplicates")

    # --- cache ---
    if cfg.cache.ttl_interval_fraction <= 0:
        problems.append("cache.ttl_interval_fraction must be positive")
    if cfg.cache.min_ttl_seconds < 0 or cfg.cache.max_ttl_seconds < cfg.cache.min_ttl_seconds:
        problems.append("cache TTL bounds invalid (need 0 <= min <= max)")

    # --- thresholds ---
    sr = cfg.signal_rules
    if not (0.0 <= sr.min_confidence <= 1.0):
        problems.append("signal_rules.min_confidence must be within [0, 1]")
    if not (0.0 <= sr.min_directional_margin <= 100.0):
        problems.append("signal_rules.min_directional_margin must be within [0, 100]")
    if not (0.0 <= sr.max_contradiction_ratio <= 1.0):
        problems.append("signal_rules.max_contradiction_ratio must be within [0, 1]")
    if not (0.0 <= sr.contradiction_penalty_weight <= 1.0):
        problems.append("signal_rules.contradiction_penalty_weight must be within [0, 1]")
    if not (0.0 <= sr.vol_regime_floor <= 1.0):
        problems.append("signal_rules.vol_regime_floor must be within [0, 1]")
    if sr.strong_min_directional < sr.moderate_min_directional:
        problems.append(
            "signal_rules.strong_min_directional must be >= moderate_min_directional"
        )

    # --- feature windows (all bar windows must be positive) ---
    fp = cfg.features
    for label, val in (
        ("ema_fast_bars", fp.ema_fast_bars),
        ("ema_slow_bars", fp.ema_slow_bars),
        ("macd_fast_bars", fp.macd_fast_bars),
        ("macd_slow_bars", fp.macd_slow_bars),
        ("macd_signal_bars", fp.macd_signal_bars),
        ("rsi_period_bars", fp.rsi_period_bars),
        ("bollinger_period_bars", fp.bollinger_period_bars),
        ("atr_period_bars", fp.atr_period_bars),
        ("range_window_bars", fp.range_window_bars),
        ("volume_window_bars", fp.volume_window_bars),
    ):
        if not isinstance(val, int) or val <= 0:
            problems.append(f"features.{label} must be a positive integer (got {val!r})")
    if fp.ema_fast_bars >= fp.ema_slow_bars:
        problems.append("features.ema_fast_bars must be < ema_slow_bars")
    for m in fp.return_window_multiples:
        if not isinstance(m, int) or m <= 0:
            problems.append(f"features.return_window_multiples entry {m!r} must be a positive int")

    # --- weights ---
    for group_name, weights in (
        (HORIZON_GROUP_SHORT, cfg.weights.short),
        (HORIZON_GROUP_EXTENDED, cfg.weights.extended_intraday),
    ):
        problems.extend(_validate_weights(group_name, weights))

    # --- payout ---
    r = cfg.payout.payout_ratio
    if r is not None:
        try:
            rv = float(r)
        except (TypeError, ValueError):
            problems.append(f"payout.payout_ratio {r!r} is not a number")
        else:
            if not (0.0 < rv <= 5.0):
                problems.append(
                    f"payout.payout_ratio {rv} out of the plausible (0, 5] range"
                )

    # --- robustness / liquidity / quality bounds ---
    if not (0.0 <= cfg.robustness.max_provider_failure_ratio <= 1.0):
        problems.append("robustness.max_provider_failure_ratio must be within [0, 1]")
    for label, val in (
        ("liquidity.max_zero_volume_ratio", cfg.liquidity.max_zero_volume_ratio),
        ("data_quality.max_missing_ratio", cfg.data_quality.max_missing_ratio),
        ("data_quality.max_duplicate_ratio", cfg.data_quality.max_duplicate_ratio),
    ):
        if not (0.0 <= float(val) <= 1.0):
            problems.append(f"{label} must be within [0, 1]")
    if not (0.0 <= cfg.data_quality.min_data_quality_score <= 100.0):
        problems.append("data_quality.min_data_quality_score must be within [0, 100]")

    # --- sentiment ---
    if cfg.sentiment.min_horizon_minutes < HORIZON_MIN_MINUTES:
        problems.append("sentiment.min_horizon_minutes must be >= 5")

    return problems


def _validate_weights(group_name: str, weights: Dict[str, float]) -> List[str]:
    problems: List[str] = []
    unknown = set(weights) - set(FEATURE_FAMILIES)
    if unknown:
        problems.append(
            f"weights.{group_name} has unknown families {sorted(unknown)}; "
            f"allowed: {list(FEATURE_FAMILIES)}"
        )
    numeric: Dict[str, float] = {}
    for fam, w in weights.items():
        try:
            wv = float(w)
        except (TypeError, ValueError):
            problems.append(f"weights.{group_name}.{fam} {w!r} is not a number")
            continue
        if wv < 0:
            problems.append(f"weights.{group_name}.{fam} must be >= 0 (got {wv})")
        numeric[fam] = wv
    total = sum(v for v in numeric.values() if v > 0)
    if total <= 0:
        problems.append(f"weights.{group_name} must have a positive total weight")
        return problems
    for fam, wv in numeric.items():
        if fam == "sentiment":
            continue
        if wv / total > MAX_SINGLE_FAMILY_FRACTION:
            problems.append(
                f"weights.{group_name}.{fam} is {wv / total:.0%} of the total; "
                f"no single family may exceed {MAX_SINGLE_FAMILY_FRACTION:.0%}"
            )
    sentiment_frac = numeric.get("sentiment", 0.0) / total
    cap = (
        MAX_SENTIMENT_FRACTION_SHORT
        if group_name == HORIZON_GROUP_SHORT
        else MAX_SENTIMENT_FRACTION_EXTENDED
    )
    if sentiment_frac > cap:
        problems.append(
            f"weights.{group_name}.sentiment is {sentiment_frac:.0%} of the total; "
            f"cap for {group_name} horizons is {cap:.0%} -- sentiment must stay a "
            "secondary input and never override live price/volume evidence"
        )
    return problems


def _valid_hhmm(value: str) -> bool:
    try:
        hh, mm = str(value).split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AttributeError):
        return False
