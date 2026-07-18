# Binary Options Selection — Roadmap

Milestone 1 (this repo state) ends at **research signal generation**:
CALL / PUT / NO_TRADE per (asset × 5–120 min horizon), with honest data
handling and no execution. Later milestones, in order of prerequisite:

## Milestone 2 — Signal persistence & outcome capture
- Persist every generated signal row (already schema-complete) to an
  append-only store keyed by `run_id`.
- A small collector that, after each expiry passes, fetches the settlement
  candle and applies the outcome definitions in `signals/labels.py`
  (CALL_WIN / PUT_WIN / TIE / UNKNOWN) — strictly separated from signal
  generation.

## Milestone 3 — Historical replay & walk-forward evaluation
- Replay stored candles through the pipeline with the injectable clock
  (`run_pipeline(..., now=...)`) to generate point-in-time signals.
- Walk-forward, out-of-sample evaluation only; explicit look-ahead guards.
- Metrics: hit rate per horizon/regime/symbol, confusion by signal strength,
  NO_TRADE opportunity cost, stability across time.

## Milestone 4 — Probability calibration
- Only after Milestone 3: train a calibrated classifier (e.g. logistic /
  isotonic on the family scores) with time-aware splits.
- Expose `predicted_probability` **only** when calibration is measured
  (reliability curves, Brier score) — the output schema and validator already
  reserve the field and currently reject any non-null value.
- Then, and only then, expected edge vs the break-even threshold
  `p* = 1/(1+r)` becomes meaningful.

## Milestone 5 — Payout & platform modeling
- Real contract terms per platform (payout ratios by asset/horizon, tie
  rules, early-close rules).
- Net-of-payout evaluation of calibrated signals.

## Milestone 6 — Better data
- Real-time or low-latency intraday provider (keyed) behind the existing
  `IntradayCandlesProvider` registry.
- Exchange holiday calendar; quote data for spread-aware liquidity screening.

> Trade execution, broker integration, and stake sizing remain intentionally
> out of scope for this repository — consistent with the equity module's
> roadmap. Martingale/loss-recovery systems will not be implemented in any
> milestone.
