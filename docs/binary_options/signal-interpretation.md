# Signal interpretation

## Canonical values

| Field | Values | Meaning |
|---|---|---|
| `signal` | `CALL` | evidence favors the settlement price finishing **above** the contract reference price at expiration |
| | `PUT` | evidence favors the settlement price finishing **below** it |
| | `NO_TRADE` | availability, freshness, economics, timing, data quality, or evidence is insufficient |
| `recommended_action` | `BUY_CALL` / `BUY_PUT` / `NO_TRADE` | the purchase of an upward or downward **contract**. Never "buy/sell the underlying"; `SELL` does not exist here because closing an open contract is platform-specific and unmodeled |

`NO_TRADE` is a first-class result — often the correct one. Every `NO_TRADE`
row carries exactly one machine-readable `no_trade_reason` plus warning
flags.

## NO_TRADE reasons (primary-reason taxonomy)

**Contract / catalog:** `INSTRUMENT_AVAILABILITY_UNKNOWN` (no verified
catalog — the default state), `INSTRUMENT_NOT_AVAILABLE`, `CATALOG_STALE`,
`EXPIRATION_NOT_AVAILABLE`, `CONTRACT_CLOSED`, `OTC_EXCLUDED`,
`PAYOUT_UNKNOWN`, `PAYOUT_STALE`, `PAYOUT_BELOW_MINIMUM`,
`PLATFORM_QUOTE_STALE`, `QUOTE_MISMATCH`.
**Market data:** `PROVIDER_UNAVAILABLE`, `DATA_NOT_REAL_TIME`,
`STALE_MARKET_DATA`, `INCOMPLETE_LATEST_CANDLE`, `INSUFFICIENT_HISTORY`,
`MISSING_CANDLES`, `DUPLICATE_CANDLES`, `INVALID_OHLC`, `LOW_DATA_QUALITY`,
`LOW_LIQUIDITY`, `ABNORMAL_MARKET_CONDITION`, `SUB_MINIMUM_PRICE`.
**Session / timing:** `MARKET_CLOSED`, `SESSION_NOT_SUPPORTED`,
`EXPIRY_BEYOND_SESSION`, `UNSUPPORTED_HORIZON`,
`TIME_TO_EXPIRY_TOO_SHORT`.
**Evidence:** `INSUFFICIENT_FEATURE_COVERAGE`, `CONTRADICTORY_EVIDENCE`,
`WEAK_DIRECTIONAL_MARGIN`, `LOW_CONFIDENCE`.

Gate order is root-cause-first: an unavailable contract reports
`INSTRUMENT_NOT_AVAILABLE`, not a downstream data symptom.

## Scores

Per row (all bounded): `call_score`/`put_score` (0–100),
`directional_score` (−100..+100, = call − put), `confidence_score` (0–1),
`data_quality_score` (0–100), `liquidity_score` (0–100), per-family
contributions (momentum, trend, mean-reversion, volume, market context,
optional capped sentiment — missing families are recorded, never
neutral-filled), `contradiction_ratio`, `signal_strength`
(strong/moderate/weak), `signal_validity` (valid/degraded/invalid), and
`ranking_validity` (actionable/excluded — invalid rows never enter the
actionable ranking).

**`confidence_score` is not a probability.** It is a bounded heuristic
(directional margin × contradiction penalty × data quality × feature
coverage × volatility regime) with **no measured accuracy** until the
[historical evaluation](evaluation.md) says otherwise. Do not compare it to
`break_even_win_probability` as if it were a win rate, and no expected value
is ever computed from it.

## Contract economics

`payout_ratio` = **net profit per unit staked** on a win (0.85 ⇒ an 85%
profit). `break_even_win_probability = 1 / (1 + payout_ratio)`; for a 0.85
payout that is `1 / 1.85 ≈ 54.1%`. It is contextual only: with a sub-1
payout you must win **more** than half the time just to break even — the
central structural difficulty of binary options.

## Timing fields

Every row carries `analysis_timestamp`, `reference_timestamp` (close of the
latest completed candle), `reference_price`, `horizon_minutes`,
`horizon_group`, `expiration_timestamp` (= reference + horizon, both aligned
to candle boundaries in UTC), `time_remaining_seconds`,
`latest_completed_candle_timestamp`, `market_data_age_seconds`, and
`catalog_age_seconds`. Historical outcome labels are `CALL_WIN`, `PUT_WIN`,
`TIE` (settlement exactly equals reference; platform tie rules vary and are
reported, not guessed), and `UNKNOWN` (no settlement candle — never counted
as a loss).
