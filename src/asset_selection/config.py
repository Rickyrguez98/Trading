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
    # Mode: "full" (no implicit cap) | "sample" (honours sample_limit) | "custom".
    mode: str = "full"
    # Only used in sample mode. None in full mode -- the staged funnel caps
    # the universe via `pipeline.after_*_top_k`, not a flat alphabetical chop.
    sample_limit: Optional[int] = None
    top_n: int = 25
    output_dir: str = "reports"
    processed_dir: str = "data/processed"

    # Legacy alias from earlier versions: a flat universe cap. Still read so
    # old YAML configs don't break, but the staged pipeline ignores it in
    # "full" mode. Mapped onto sample_limit when mode == 'sample'.
    max_tickers: Optional[int] = None


@dataclass
class PipelineStagesConfig:
    """How many tickers each stage of the funnel keeps.

    None disables the cap for that stage (keep everything that passes the
    stage's filters). In a full-universe run, the typical pattern is:
        Stage 1 -> ~4700 cleaned commons (no cap)
        Stage 2 -> after_prices_top_k ~ 500   (liquidity + price prescreen)
        Stage 3 -> after_fundamentals_top_k ~ 150 (fundamental prescreen)
        Stage 4 -> news/sentiment only for those 150
        Stage 5 -> rank, top_n into the Markdown report
    """
    after_prices_top_k: Optional[int] = 500
    after_fundamentals_top_k: Optional[int] = 150
    # Minimum price history (number of daily closes) before a ticker is kept.
    min_price_history_days: int = 30
    # Stage-1 hard cap. Default None means keep the full cleaned universe.
    # Useful for very large samples / regression runs.
    universe_max: Optional[int] = None


@dataclass
class UniverseConfig:
    sources: List[str] = field(default_factory=lambda: ["nasdaq_trader", "sec_company_tickers"])

    # New: exchange whitelist + asset-type include flags.
    # An empty `exchanges` list keeps every exchange seen in the source data.
    # `include_*` toggles default to False (exclude) for fund-like and
    # non-common-stock asset types.
    exchanges: List[str] = field(default_factory=list)
    include_etfs: bool = False
    include_funds: bool = False
    include_warrants: bool = False
    include_units: bool = False
    include_preferred: bool = False
    include_rights: bool = False
    include_test_issues: bool = False
    include_notes: bool = False
    # Temporary / conditional instruments: "When-Issued" (WI) lines that trade
    # before a corporate action settles, and other temporary/special tickers.
    # Their price history is short and not comparable to seasoned common stock,
    # so they are excluded by default.
    include_when_issued: bool = False

    # Legacy aliases. If a user's YAML uses the old `exclude_*` form we honour
    # it (e.g. `exclude_etfs: false` == `include_etfs: true`). New code reads
    # only the `include_*` form via `effective_include`.
    exclude_etfs: Optional[bool] = None
    exclude_warrants: Optional[bool] = None
    exclude_units: Optional[bool] = None
    exclude_preferred: Optional[bool] = None
    exclude_rights: Optional[bool] = None
    exclude_test_issues: Optional[bool] = None

    min_ticker_length: int = 1
    max_ticker_length: int = 5

    def effective_include(self, asset_kind: str) -> bool:
        """Return whether ``asset_kind`` (e.g. 'etfs', 'warrants') is included.

        Honours the legacy ``exclude_*`` form when present; otherwise uses
        the new ``include_*`` form.
        """
        legacy = getattr(self, f"exclude_{asset_kind}", None)
        if legacy is not None:
            return not bool(legacy)
        return bool(getattr(self, f"include_{asset_kind}", False))


@dataclass
class ProvidersConfig:
    fundamentals: str = "yfinance"
    prices: str = "yfinance"
    news: str = "yfinance"


@dataclass
class RobustnessConfig:
    """Backup-plan + provider-fallback policy.

    The pipeline tries primary -> secondary provider(s) (live), then a
    fresh-enough cache (Plan C), then -- if everything fails -- produces a
    diagnostic-only result rather than a misleading ranking (Plan D). Coverage
    thresholds (consumed by the coverage-validation layer) gate whether the
    final ranking is presented as valid, partial, or diagnostic.
    """
    # --- Backup-plan ladder ---
    # On a live provider miss, may we serve a recent-but-expired cache entry?
    use_cache_on_provider_failure: bool = False
    # How old (days) a cache entry may be and still back a failed live fetch.
    max_cache_age_days: float = 7.0
    # Allow stale cache to back a *diagnostic* run even if not used live.
    allow_stale_cache_for_diagnostic: bool = True
    # Ordered provider names per data type, e.g.
    # {"prices": ["yfinance", "stooq"]}. Empty -> use providers.<type> alone.
    provider_priority_by_data_type: Dict[str, List[str]] = field(default_factory=dict)
    # If benchmark health shows a systemic provider failure, refuse to present
    # a normal ranking (the run is marked INVALID_PROVIDER_FAILURE).
    stop_on_systemic_provider_failure: bool = True
    # Allow a degraded-but-usable run to be presented as a PARTIAL ranking
    # (with warnings) instead of being blocked outright.
    allow_partial_ranking: bool = True

    # --- Coverage thresholds (consumed by validation/coverage.py) ---
    # Minimum fraction of priced/fundamentals/news coverage required before the
    # ranking is trusted. News coverage never blocks a run; it only downgrades
    # sentiment confidence.
    min_price_coverage_ratio: float = 0.60
    min_fundamentals_coverage_ratio: float = 0.50
    min_news_coverage_ratio: float = 0.10
    # If provider-side failures exceed this fraction of attempts, treat as a
    # systemic outage.
    max_provider_failure_ratio: float = 0.50
    # Fewest valid-fundamentals candidates required to present a real ranking.
    min_valid_candidates_for_ranking: int = 1
    # Minimum benchmark price-health success ratio to run the full pipeline.
    min_benchmark_price_health_ratio: float = 0.50

    @property
    def max_cache_age_seconds(self) -> int:
        return int(max(0.0, self.max_cache_age_days) * 86400)


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
    # Confidence model. The old model saturated at 3 articles, so the common
    # yfinance case (10 near-identical headlines from one wire) reached
    # confidence 1.0 -- clearly overstated. The new model needs many UNIQUE
    # articles from DIVERSE sources, recent and de-duplicated, to approach 1.0.
    # Articles needed to saturate the count factor (10 != full confidence).
    confidence_full_article_count: int = 25
    # Distinct sources needed to saturate the diversity factor.
    confidence_full_source_count: int = 5
    # Articles whose published_at is older than this are counted as "stale";
    # a high stale ratio lowers confidence.
    stale_after_days: float = 14.0
    # Per-model confidence ceiling. A lexicon model (VADER) is noisier than a
    # finance-tuned transformer, so it can never be as confident as FinBERT.
    vader_confidence_factor: float = 0.85
    finbert_confidence_factor: float = 1.0

    # --- Confidence-adjusted sentiment (fed into the composite) ---
    # When true, the composite uses an EFFECTIVE sentiment that is pulled toward
    # `neutral_sentiment_score` in proportion to (1 - confidence): a low-confidence
    # 80/100 sentiment contributes far less than a high-confidence one. The raw
    # sentiment is still kept (raw_sentiment_score) for transparency.
    use_confidence_adjusted_sentiment: bool = True
    neutral_sentiment_score: float = 50.0
    # --- Stale-news handling (consumed in flag_rows + effective confidence) ---
    # A low fresh_ratio damps the effective confidence used above, so sentiment
    # that leans on aging coverage cannot swing the score.
    stale_news_penalty_enabled: bool = True
    # fresh_ratio below this fires STALE_NEWS and starts damping confidence.
    stale_news_fresh_ratio_threshold: float = 0.50
    # fresh_ratio at/below this fires VERY_STALE_NEWS.
    very_stale_news_fresh_ratio_threshold: float = 0.20
    # Distinct non-duplicate sources below this fires LOW_SOURCE_DIVERSITY.
    low_source_diversity_threshold: int = 2


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
class RiskControlsConfig:
    """Thresholds for volatility/risk flags and the ``selection_bucket`` label.

    We never auto-remove a candidate for being volatile -- we *label* it so a
    reader can tell a steady compounder from a 100%-vol momentum name. All
    volatility values are annualized fractions (0.80 == 80%).
    """
    # Above this annualized volatility -> HIGH_VOLATILITY flag + speculative.
    max_volatility_pct: float = 0.80
    # Above this risk_penalty (0..100) -> speculative.
    max_risk_penalty: float = 50.0
    # SPECULATIVE_MOMENTUM fires when BOTH return and volatility clear these
    # gates (a big run-up on a very noisy tape).
    speculative_return_pct: float = 0.50
    speculative_volatility_pct: float = 0.60
    # high_quality_core requires strong fundamentals, contained vol, low risk.
    core_min_fundamentals: float = 58.0
    core_max_volatility_pct: float = 0.45
    core_max_risk_penalty: float = 12.0
    # Below this fundamentals score (and not already speculative) -> watchlist.
    watchlist_max_fundamentals: float = 50.0


@dataclass
class AllocationConfig:
    """Gates that separate a *research ranking* from an *allocation shortlist*.

    Asset selection answers "which assets are worth considering?". These knobs
    decide which of those research candidates are *eligible* to be handed to a
    future allocation/rebalancing module (which answers "how much, and when?").
    Nothing here removes a candidate from the research ranking -- it only sets
    ``eligible_for_allocation`` and the ``allocation_adjusted_score``.

    Default policy: only ``high_quality_core_candidate`` and ``growth_candidate``
    can be eligible, and only if they also clear the risk / data-quality /
    sentiment gates below. ``speculative_candidate`` and ``watchlist_only`` are
    never eligible by default (flip the ``allow_*`` switches to override).
    """
    # --- Bucket gating ---
    allow_speculative_for_allocation: bool = False
    allow_watchlist_for_allocation: bool = False

    # --- Risk gating (annualized vol fraction; risk_penalty on 0..100) ---
    max_allocation_volatility: float = 0.50
    max_allocation_risk_penalty: float = 25.0
    require_non_negative_recent_return_for_allocation: bool = True

    # --- Data-quality gating ---
    max_missing_metric_count_for_allocation: int = 4
    require_market_cap_for_allocation: bool = True

    # --- Sentiment gating (only applied when the name actually has news) ---
    min_sentiment_confidence_for_allocation: float = 0.30
    min_fresh_news_ratio_for_allocation: float = 0.50

    # --- allocation_adjusted_score penalties (subtracted from final_score) ---
    penalty_high_volatility: float = 25.0
    penalty_speculative_momentum: float = 20.0
    penalty_weak_price_trend: float = 15.0
    penalty_watchlist_bucket: float = 20.0
    penalty_speculative_bucket: float = 15.0
    penalty_low_sentiment_confidence: float = 10.0
    penalty_stale_news: float = 10.0
    # Multiplier on (risk_penalty - max_allocation_risk_penalty) when over budget.
    penalty_excess_risk_weight: float = 0.5


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_to_file: bool = False
    file: str = "logs/asset_selection.log"


@dataclass
class AppConfig:
    run: RunConfig = field(default_factory=RunConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    pipeline: PipelineStagesConfig = field(default_factory=PipelineStagesConfig)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    robustness: RobustnessConfig = field(default_factory=RobustnessConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    rate_limits: Dict[str, float] = field(default_factory=lambda: {"yfinance": 0.4})
    prices: PricesConfig = field(default_factory=PricesConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    composite: CompositeConfig = field(default_factory=CompositeConfig)
    risk_controls: RiskControlsConfig = field(default_factory=RiskControlsConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
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
        pipeline=PipelineStagesConfig(**_filtered(PipelineStagesConfig, section("pipeline"))),
        providers=ProvidersConfig(**_filtered(ProvidersConfig, section("providers"))),
        robustness=RobustnessConfig(**_filtered(RobustnessConfig, section("robustness"))),
        cache=CacheConfig(**_filtered(CacheConfig, section("cache"))),
        rate_limits=dict(section("rate_limits") or {"yfinance": 0.4}),
        prices=PricesConfig(**_filtered(PricesConfig, section("prices"))),
        sentiment=SentimentConfig(**_filtered(SentimentConfig, section("sentiment"))),
        scoring=ScoringConfig(**_filtered(ScoringConfig, section("scoring"))),
        composite=CompositeConfig(**_filtered(CompositeConfig, section("composite"))),
        risk_controls=RiskControlsConfig(**_filtered(RiskControlsConfig, section("risk_controls"))),
        allocation=AllocationConfig(**_filtered(AllocationConfig, section("allocation"))),
        logging=LoggingConfig(**_filtered(LoggingConfig, section("logging"))),
    )


def _filtered(cls, data: Dict[str, Any]) -> Dict[str, Any]:
    # Tolerate unknown keys in the YAML so adding a field doesn't break older configs.
    valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in valid}
