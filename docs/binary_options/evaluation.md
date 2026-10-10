# Historical evaluation (research-only)

Module: `binary_options_selection.evaluation` — never imported by the live
pipeline (enforced by a test). It answers "would these signals have worked
historically?" with strict anti-leakage rules.

## Design guarantees

- **Chronological replay.** At each evaluation timestamp the features,
  scores, and decision see only `candles[: i + 1]`; the future slice is used
  *exclusively* for outcome resolution through the isolated label rules,
  which reject any pre-reference candle.
- **Time-based splits.** Records are assigned train → validation → test in
  timestamp order; never shuffled. No tuning loop exists, so the test period
  is untouched — thresholds must not be optimized against it.
- **Honest outcomes.** `CALL_WIN` / `PUT_WIN` / `TIE` / `UNKNOWN`; an
  `UNKNOWN` (missing settlement candle) is excluded from return arithmetic —
  it is not a loss.
- **Explicit payout assumption.** Win → `+payout_ratio`, loss → `−1`,
  tie → `0` per unit staked; the same assumption applies to the strategy and
  every baseline.

## Reported metrics

Counts (signal/call/put/no-trade), `coverage_rate`, `no_trade_rate`,
`call_win_rate`, `put_win_rate`, `combined_win_rate`, `tie_rate`,
`unknown_rate`, `average_payout`, `break_even_win_rate`,
`realized_return_per_unit_staked`, `maximum_consecutive_losses`,
`maximum_drawdown`, and slices by horizon, horizon group, asset type,
session, **confidence bucket**, and **signal strength**. Because the
confidence score is a heuristic, no calibration metrics (Brier, reliability
curves) are produced — the bucket table shows whether higher confidence
*historically* corresponded to better outcomes, nothing more.

## Baselines

Evaluated on the identical grid and payout assumption: always CALL, always
PUT, previous-candle direction, simple momentum, random with a fixed seed.
`compare_to_baselines` renders a verdict that only permits an
"outperformed" statement on the untouched out-of-sample split with
sufficient observations; otherwise it states plainly that **no predictive
power can be claimed**.

## Usage (API)

```python
from binary_options_selection.config import load_config
from binary_options_selection.evaluation import (
    EvaluationConfig, evaluate_with_baselines, full_report, compare_to_baselines,
)

cfg = load_config("configs/binary_options/default.yaml")
ec = EvaluationConfig(horizons=[5, 10, 15, 30, 60, 90, 120],
                      step_bars=6, payout_ratio=0.85)
records = evaluate_with_baselines(candles, cfg, ec)   # candles: List[Candle]
reports = {name: full_report(recs, ec) for name, recs in records.items()}
verdict = compare_to_baselines(reports, split="test")
```

Historical candle data must be supplied by the user (the default free feed
only reaches ~60 days back at intraday resolution and is delayed).

## Deferred to future milestones

Probability-model training and calibration (only after which
`predicted_probability` and expected value become legitimate), platform-tie
rules and per-platform payout modeling, multi-symbol portfolio evaluation,
and walk-forward threshold selection with a preserved final holdout.
