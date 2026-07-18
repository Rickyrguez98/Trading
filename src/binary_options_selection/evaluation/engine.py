"""Research-only historical evaluation (chronological replay).

Answers, honestly, "would these signals have worked?" without any look-ahead:

* candles are replayed **chronologically**; at each evaluation timestamp the
  features/scores/decision see only ``candles[: i + 1]`` — the future slice is
  used *exclusively* to resolve the outcome via the isolated label rules
  (``signals.labels``), which themselves reject pre-reference candles;
* splits are **time-based** (train → validation → test in order, never
  shuffled); the test period is untouched by any tuning loop — none exists in
  this milestone, and thresholds must not be optimized against it;
* outcomes distinguish ``UNKNOWN`` (missing settlement candle) from losses;
* the payout assumption is explicit and applied identically to the strategy
  and every baseline.

The pipeline's heuristic ``confidence_score`` is NOT a probability; this
module therefore reports whether higher confidence buckets *historically*
produced better outcomes instead of computing calibration metrics that would
presuppose a probability model.

This module is research tooling: it is never imported by the live pipeline.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from ..config import BinaryOptionsConfig, horizon_group
from ..data_providers.base import Candle
from ..features.engine import compute_features
from ..market_session import MARKET_OPEN, SessionState, expiry_for
from ..scoring.directional import score_direction
from ..signals.decision import (
    SIGNAL_CALL,
    SIGNAL_NO_TRADE,
    SIGNAL_PUT,
    assess_liquidity,
    decide_signal,
)
from ..signals.labels import CALL_WIN, PUT_WIN, TIE, UNKNOWN, resolve_outcome
from ..utils.timeutils import candle_close_time
from ..validation.candles import assess_series
from ..data_providers.base import IntradaySeries


@dataclass
class EvaluationConfig:
    horizons: List[int] = field(default_factory=lambda: [5, 10, 15, 30, 60, 90, 120])
    step_bars: int = 6                    # evaluate every N completed bars
    # Transaction payout assumption (net profit per unit staked), applied to
    # strategy AND baselines alike.
    payout_ratio: float = 0.85
    train_fraction: float = 0.6
    validation_fraction: float = 0.2      # remainder = untouched test period
    confidence_bucket_edges: Sequence[float] = (0.25, 0.5, 0.75)
    random_seed: int = 7                  # for the random baseline only
    asset_type: str = "equity"
    session_label: str = "regular"


@dataclass
class EvalRecord:
    timestamp: str
    horizon_minutes: int
    horizon_group: str
    signal: str
    confidence: float
    strength: str
    outcome: str                          # CALL_WIN | PUT_WIN | TIE | UNKNOWN
    correct: Optional[bool]               # None when NO_TRADE or UNKNOWN
    payout_ratio: float
    split: str = "train"                  # train | validation | test


def _direction_correct(signal: str, outcome: str) -> Optional[bool]:
    if signal == SIGNAL_NO_TRADE or outcome in (UNKNOWN, TIE):
        return None
    return (signal == SIGNAL_CALL and outcome == CALL_WIN) or (
        signal == SIGNAL_PUT and outcome == PUT_WIN
    )


def replay(
    candles: List[Candle],
    cfg: BinaryOptionsConfig,
    eval_cfg: EvaluationConfig,
    *,
    signal_fn: Optional[Callable[[List[Candle], int], str]] = None,
    label: str = "strategy",
) -> List[EvalRecord]:
    """Chronological replay over one candle series.

    ``signal_fn`` overrides the strategy (used for baselines): it receives the
    *history-only* window and the horizon and returns a signal string. When it
    is None the real feature -> scoring -> decision path runs.

    Historical candles only exist while the venue traded, so replay uses an
    open-session state; every other data gate (history, quality, liquidity,
    staleness relative to the replayed clock) still applies.
    """
    interval_minutes = cfg.candles.interval_minutes
    interval_seconds = interval_minutes * 60
    records: List[EvalRecord] = []
    session = SessionState(MARKET_OPEN, None, cfg.market_session.timezone, True)
    min_bars = cfg.candles.required_history_bars

    for i in range(min_bars, len(candles), max(1, eval_cfg.step_bars)):
        history = candles[: i + 1]          # inclusive of bar i -- never beyond
        future = candles[i + 1:]
        ref_time = candle_close_time(history[-1].start, interval_seconds)
        ref_price = history[-1].close

        for horizon in eval_cfg.horizons:
            if horizon % interval_minutes != 0:
                continue
            if signal_fn is not None:
                signal = signal_fn(history, horizon)
                confidence, strength = 0.0, "baseline"
            else:
                series = IntradaySeries(
                    symbol=label, interval=cfg.candles.interval,
                    interval_seconds=interval_seconds, candles=list(history),
                    status="ok", provider="replay", data_source="live",
                )
                quality = assess_series(series, cfg, ref_time)
                bundle = compute_features(
                    quality.candles, horizon_minutes=horizon,
                    interval_minutes=interval_minutes, cfg=cfg,
                )
                scores = score_direction(
                    bundle, data_quality_score=quality.data_quality_score, cfg=cfg
                )
                decision = decide_signal(
                    scores, quality, session, assess_liquidity(quality.candles, cfg),
                    expiry_utc=expiry_for(ref_time, horizon), cfg=cfg,
                )
                signal, confidence, strength = (
                    decision.signal, scores.confidence_score, decision.signal_strength
                )

            outcome = resolve_outcome(
                future,
                reference_time=ref_time,
                reference_price=ref_price,
                expiry_time=expiry_for(ref_time, horizon),
                interval_seconds=interval_seconds,
            ).outcome
            records.append(EvalRecord(
                timestamp=ref_time.isoformat(),
                horizon_minutes=horizon,
                horizon_group=horizon_group(horizon),
                signal=signal,
                confidence=confidence,
                strength=strength,
                outcome=outcome,
                correct=_direction_correct(signal, outcome),
                payout_ratio=eval_cfg.payout_ratio,
            ))

    _assign_splits(records, eval_cfg)
    return records


def _assign_splits(records: List[EvalRecord], eval_cfg: EvaluationConfig) -> None:
    """Time-ordered train/validation/test assignment (never shuffled)."""
    stamps = sorted({r.timestamp for r in records})
    if not stamps:
        return
    n = len(stamps)
    train_end = stamps[max(0, int(n * eval_cfg.train_fraction) - 1)]
    val_end = stamps[
        max(0, int(n * (eval_cfg.train_fraction + eval_cfg.validation_fraction)) - 1)
    ]
    for r in records:
        if r.timestamp <= train_end:
            r.split = "train"
        elif r.timestamp <= val_end:
            r.split = "validation"
        else:
            r.split = "test"


# ---------------------------------------------------------------------------
# Baselines (evaluated on the identical replay grid + payout assumption)
# ---------------------------------------------------------------------------

def baseline_always_call(history: List[Candle], horizon: int) -> str:
    return SIGNAL_CALL


def baseline_always_put(history: List[Candle], horizon: int) -> str:
    return SIGNAL_PUT


def baseline_previous_candle(history: List[Candle], horizon: int) -> str:
    if len(history) < 2:
        return SIGNAL_NO_TRADE
    if history[-1].close > history[-2].close:
        return SIGNAL_CALL
    if history[-1].close < history[-2].close:
        return SIGNAL_PUT
    return SIGNAL_NO_TRADE


def baseline_simple_momentum(history: List[Candle], horizon: int, bars: int = 6) -> str:
    if len(history) <= bars:
        return SIGNAL_NO_TRADE
    delta = history[-1].close - history[-1 - bars].close
    if delta > 0:
        return SIGNAL_CALL
    if delta < 0:
        return SIGNAL_PUT
    return SIGNAL_NO_TRADE


def make_baseline_random(seed: int) -> Callable[[List[Candle], int], str]:
    rng = random.Random(seed)

    def _rand(history: List[Candle], horizon: int) -> str:
        return SIGNAL_CALL if rng.random() < 0.5 else SIGNAL_PUT

    return _rand


BASELINES: Dict[str, Callable[..., Callable]] = {
    "always_call": lambda cfg: baseline_always_call,
    "always_put": lambda cfg: baseline_always_put,
    "previous_candle": lambda cfg: baseline_previous_candle,
    "simple_momentum": lambda cfg: baseline_simple_momentum,
    "random_fixed_seed": lambda cfg: make_baseline_random(cfg.random_seed),
}


def evaluate_with_baselines(
    candles: List[Candle],
    cfg: BinaryOptionsConfig,
    eval_cfg: EvaluationConfig,
) -> Dict[str, List[EvalRecord]]:
    """Replay the strategy and every naive baseline on the identical grid."""
    out = {"strategy": replay(candles, cfg, eval_cfg)}
    for name, factory in BASELINES.items():
        out[name] = replay(
            candles, cfg, eval_cfg, signal_fn=factory(eval_cfg), label=name
        )
    return out
