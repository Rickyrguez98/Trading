# Binary Options Selection — Methodology

> Research output only. Not financial advice. Scores are transparent
> heuristics, **not** calibrated probabilities. See
> [`limitations.md`](limitations.md).

This document records every formula and threshold that materially affects a
signal. Defaults live in [`configs/binary_options_config.yaml`](../configs/binary_options_config.yaml)
and are validated at load time.

## 1. Horizons

Configurable expirations between **5 and 120 minutes** (defaults: 5, 10, 15,
30, 60, 90, 120). Groups: `short_term` = **5, 10, 15** minutes;
`extended_intraday` = **30, 60, 90, 120** minutes (30 belongs to extended by
product definition; both groups are intraday analysis, not long-term
investing). Every horizon must be a whole multiple of the candle interval so
the expiration lands on a candle boundary. Each (instrument, horizon) pair is
scored independently — feature windows scale with the horizon and weights
differ by group.

## 2. Reference and expiration times

* Internal timestamps are aware-UTC. A candle stamped `t` covers
  `[t, t + interval)` and is complete only when `t + interval ≤ now`.
* `reference_time` = close time of the latest **completed** candle;
  `reference_price` = that candle's close.
* `expiry_time = reference_time + horizon`.
* Data age = `now − reference_time`; if it exceeds
  `candles.max_staleness_intervals × interval` (default 3 intervals) the row
  is stale and can only be `NO_TRADE (STALE_DATA)`.
* An in-progress candle is dropped unless
  `candles.allow_incomplete_last_candle: true` (research replays only).

## 3. Data-quality score (0–100)

From `validation/candles.py`, after dropping duplicates, future-stamped bars,
and impossible OHLC rows (never repaired or imputed):

```
score = 100
        − 60·missing_ratio            (missing bars / expected bars, session-aware)
        − 40·duplicate_ratio
        − 10·out_of_order_count       (cap 30)
        − 15·impossible_ohlc_count    (cap 45)
        − 10·abnormal_return_count    (cap 30; |1-bar return| > 20%)
        − 20·excess_zero_volume_ratio
capped at 40 if stale, at 30 if history < required bars; clamped to [0, 100].
```

Rows below `data_quality.min_data_quality_score` (60) cannot carry a live
signal. Gaps longer than 4 h between candles count as session breaks
(overnight/weekend), not missing data — the staleness gate covers real
outages.

## 4. Feature families

Each **directional** family produces a signed score `s_f ∈ [-1, +1]`
(+ = bullish) as the mean of its available components. A family with no
computable component is **missing** — excluded from the weights, never
neutral-filled. Windows are in bars; `horizon_bars = horizon / interval`.

### momentum (windows scale with the horizon)
* `normalized_return(w)` for `w = horizon_bars × m`, `m ∈ {1, 3, 6}`:
  `clip( (close/close[-w] − 1) / (σ₁·√w) / 2, ±1)` where σ₁ is the stdev of
  1-bar returns (a 2σ move saturates the score).
* `consecutive_direction`: signed streak of same-direction closes / 5.
* `range_position`: `2·(close − low₂₀)/(high₂₀ − low₂₀) − 1`.

### trend
* `ema_gap = clip((EMA₉ − EMA₂₁) / ATR₁₄, ±1)`
* `macd_hist = clip(MACD_hist(12,26,9) / (0.5·ATR₁₄), ±1)`
* `sma_slope = clip((SMA₉ − SMA₉[−horizon_bars]) / (ATR₁₄ · max(1, horizon_bars/9)), ±1)`

### mean_reversion (contrarian)
* `rsi_pressure`: RSI₁₄ ≥ 70 → `−(RSI−70)/30`; ≤ 30 → `+(30−RSI)/30`; else 0.
* `bollinger_reversion`: with `z = (close − SMA₂₀)/σ₂₀`, when `|z| > 1`:
  `−sign(z)·clip((|z|−1)/(k−1), 0, 1)`, `k = 2`; else 0.

### volume (confirmation, signed by the last bar's direction)
* `rel_volume = volume / mean(volume over prior 20 bars)`
* score = `sign(last close move) · clip((rel_volume − 1)/(1.5 − 1), 0, 1)`

### context (benchmark, default SPY, same candle interval)
* `normalized_return(w)` on the benchmark for `m ∈ {1, 3}`. Missing when the
  benchmark series is unavailable or stale (run becomes DEGRADED).

### sentiment (optional, secondary by construction)
* Only source: the equity module's `sentiment_score` (0–100) when the
  universe mode is `asset-selection` and `sentiment.enabled: true`.
* Only applies to horizons ≥ `sentiment.min_horizon_minutes` (60): stale,
  broad news tone must not drive 5–30 minute signals.
* `news_tilt = clip((sentiment_score − 50) / 25, ±1)`; family weight capped
  at 5% (short) / 15% (extended) of total by config validation.

### volatility (non-directional)
With `range_ratio = current bar range / median range over prior 20 bars`:
* `≥ 5.0` → abnormal: `vol_regime_factor = 0.5`, flag `ABNORMAL_RANGE`;
* `≥ 2.0` → expansion: factor interpolates from 1 → 0.5, flag
  `VOLATILITY_EXPANSION`;
* `≤ 0.5` → compression: factor 0.85, flag `VOLATILITY_COMPRESSION`;
* else factor 1.0.
Candle structure (body/range, wick proportions) is reported descriptively.

## 5. Directional scores

Weights `w_f` per horizon group (defaults below), renormalized over available
families:

| family | short_term (5–15 m) | extended_intraday (30–120 m) |
|---|---|---|
| momentum | 0.30 | 0.20 |
| trend | 0.20 | 0.30 |
| mean_reversion | 0.15 | 0.10 |
| volume | 0.20 | 0.15 |
| context | 0.15 | 0.20 |
| sentiment | 0.00 | 0.05 |

Validation rejects any non-sentiment family above 60% of total weight and
sentiment above 5%/15% per group.

```
call_raw          = Σ_f w_f · max(s_f, 0)
put_raw           = Σ_f w_f · max(−s_f, 0)
call_score        = 100 · call_raw                 ∈ [0, 100]
put_score         = 100 · put_raw                  ∈ [0, 100]
directional_score = call_score − put_score         ∈ [−100, +100]
contradiction     = min(call_raw, put_raw) / max(call_raw, put_raw)   (0 if both 0)

confidence_score  = (|directional_score| / 100)
                    × (1 − 0.5 · contradiction)
                    × (data_quality_score / 100)
                    × feature_coverage             (available weight / configured weight)
                    × vol_regime_factor
```

CALL/PUT are symmetric by construction: negating every family score swaps
`call_score` and `put_score` exactly (tested). `confidence_score` is a
bounded heuristic in [0, 1]; nothing in this module is named or treated as a
probability.

## 6. Decision rules

**Contract gates first** (a signal on an untradable contract is
meaningless): `UNSUPPORTED_HORIZON`, then — from the verified catalog —
`INSTRUMENT_AVAILABILITY_UNKNOWN` (no catalog; the default state),
`CATALOG_STALE`, `INSTRUMENT_NOT_AVAILABLE`, `CONTRACT_CLOSED`,
`OTC_EXCLUDED` (default policy; allowed OTC rows always warn),
`EXPIRATION_NOT_AVAILABLE`, `PAYOUT_UNKNOWN` / `PAYOUT_STALE` /
`PAYOUT_BELOW_MINIMUM`, `PLATFORM_QUOTE_STALE`, `QUOTE_MISMATCH` (platform
vs market divergence in bps). `--research-mode` downgrades the
unknown-availability/economics gates to visible warnings, never the
positively bad states.

**Market-data gates** force `NO_TRADE` with `signal_validity = invalid`, in
root-cause order: `PROVIDER_UNAVAILABLE`, `INCOMPLETE_LATEST_CANDLE`,
`INSUFFICIENT_HISTORY` (< 60 bars), `STALE_MARKET_DATA`,
`DATA_NOT_REAL_TIME` (known provider delay > 0.5 × horizon — yfinance's
~900 s delay honestly rejects every 5/10/15-minute signal),
`TIME_TO_EXPIRY_TOO_SHORT` (data age leaves < 75% of the horizon),
`INVALID_OHLC` (impossible OHLC / future stamps),
`ABNORMAL_MARKET_CONDITION` (abnormal-range bar), `SUB_MINIMUM_PRICE`
(≤ $1), `LOW_DATA_QUALITY` / `MISSING_CANDLES` (< 60 score),
`LOW_LIQUIDITY` (volume gates apply only to asset types whose feeds carry
real volume), `SESSION_NOT_SUPPORTED` (unmodeled venue), `MARKET_CLOSED`,
`EXPIRY_BEYOND_SESSION`. Sessions are asset-type aware: crypto 24/7, forex
24/5 (documented approximation), equity/ETF/index exchange hours.

**Evidence gates** keep the data validity (a NO_TRADE on clean data is an
answer): `INSUFFICIENT_FEATURE_COVERAGE`, `CONTRADICTORY_EVIDENCE`
(contradiction > 0.60), `WEAK_DIRECTIONAL_MARGIN` (|directional| < 15),
`LOW_CONFIDENCE` (confidence < 0.25).

Otherwise: `CALL` if directional > 0 else `PUT`, with
`recommended_action = BUY_CALL / BUY_PUT` (contract purchases — never
trading the underlying). Strength: **strong** when
|directional| ≥ 40 **and** confidence ≥ 0.50; **moderate** when
|directional| ≥ 25; else **weak**.

## 7. Ranking

Actionable rows sort by (confidence desc, |directional| desc, symbol asc,
horizon asc) — fully deterministic, ties always break the same way. NO_TRADE
rows are listed separately and never interleaved with ranked signals.

## 8. Payout context

With a configured payout ratio `r` (net win per unit staked), the break-even
win probability is `p* = 1/(1 + r)` (e.g. r = 0.85 → p* ≈ 52.6%). This is
context only. No expected value or edge is ever computed from
`confidence_score`; `expected_edge` exists in code solely to refuse
non-calibrated inputs.

## 9. Outcome definitions (future evaluation)

For later backtesting/calibration (`signals/labels.py`, never imported by the
live pipeline): settlement = close of the candle closing exactly at
`expiry_time`; `CALL_WIN` if settlement > reference, `PUT_WIN` if <, `TIE`
if =, `UNKNOWN` if the settlement candle is missing (no nearest-candle
guessing unless an explicit, visible tolerance is passed). Candles before the
reference time are rejected to keep signal data and outcome data separated.
