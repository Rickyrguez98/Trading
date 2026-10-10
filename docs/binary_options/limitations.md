# Binary Options Selection — Limitations

Read this before using any output. These are structural limitations, stated
plainly so the signals are interpreted for what they are.

## The instrument itself

- **Binary options are high-risk, all-or-nothing instruments.** Many retail
  platforms operate offshore under weak or no regulation; several regulators
  (SEC/CFTC, ESMA) have warned about or banned retail binary options
  outright. Nothing in this module vets platforms, contracts, or
  counterparty risk.
- **The payout structure works against the trader.** With a typical payout
  ratio r < 1, the break-even win probability 1/(1+r) exceeds 50%. A signal
  that is "right more often than not" can still lose money.

## The scores

- **`confidence_score` is not a probability.** It is a bounded heuristic
  combining signal margin, contradiction, data quality, feature coverage, and
  the volatility regime. It has **no measured accuracy**: no backtest, no
  hit-rate, no calibration exists in this milestone. Do not compare it to the
  break-even threshold as if it were a win rate.
- **Technical indicators have no guaranteed predictive power.** Intraday
  price direction over 5–120 minutes is extremely noisy; sub-hour moves are
  close to coin-flips after costs for most liquid names. The default
  thresholds are engineering choices, **not** optimized parameters — they
  have deliberately not been tuned against historical samples (tuning on a
  small sample would overfit and mislead).
- **Expected value is never computed** because it would be fiction without a
  calibrated probability and actual contract terms.

## The data

- **The default feed is delayed.** yfinance intraday data typically lags the
  tape (~15 min). The staleness gate reports this honestly (rows go
  `NO_TRADE / STALE_DATA`), which means the default free setup is suitable
  for **research and architecture validation, not live signal timing**. A
  real-time provider is required for the latter.
- **No exchange holiday calendar.** Holidays classify as normal weekdays; the
  staleness gate is what prevents signals on a closed tape.
- **No bid/ask spread data.** Liquidity is screened via volume and dollar
  volume per bar; a spread proxy is only honest with quote data, which the
  free feed does not provide.
- **Session-aware gap counting is conservative.** Gaps longer than 4 h count
  as session breaks, not missing data; pathological multi-hour intraday
  outages are caught by staleness, not the missing-bar counter.

## The evaluation

- **No backtesting has been performed.** Outcome definitions exist
  (`signals/labels.py`) precisely so a future milestone can evaluate stored
  signals without look-ahead, but as of this milestone **no accuracy claim
  of any kind is made**.
- Sentiment, when enabled, comes from the equity module's news scoring —
  broad and possibly hours old; that is why it is excluded from short
  horizons and capped for extended ones.

## The scope

- No execution, no broker connectivity, no order placement, no credentials,
  no stake sizing, no martingale/loss-recovery logic — by design, and the
  roadmap keeps it that way.
