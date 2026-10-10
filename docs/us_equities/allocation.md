# Portfolio allocation — methodology

> Research output only — **not financial advice**, not a recommendation to buy
> or sell anything, and not an optimized or backtested portfolio. See
> [../shared/disclaimer.md](../shared/disclaimer.md).

Turns the asset-selection ranking into a sized portfolio in three risk
strategies, reporting every position in **percentage and money**. Why this
architecture was chosen (and what was rejected):
[allocation-readiness.md](allocation-readiness.md).

## 1. Sleeves

A portfolio is a budget over six sleeves. The budget always covers the whole
portfolio and sums to 1.0 — cash included.

| Sleeve | Held through | Source |
| --- | --- | --- |
| `cash` | — | residual liquidity |
| `bond_proxy` | one broad bond ETF (`BND` by default) | configuration |
| `index_core` | one broad S&P 500 ETF (`SPLG` by default) | configuration |
| `equity_core` | single names | ranking, `candidate_role = core_candidate` |
| `growth_satellite` | single names | ranking, `satellite_growth_candidate` |
| `speculative` | single names | ranking, `speculative_research_only` (opt-in) |

The equity sleeves are filled **from the ranking**, not from a hard-coded
list. That is the link between selection and allocation: selection decides
*which* names and *how attractive*; allocation decides *how much*.

`index_core` and `bond_proxy` are labeled sector `BROAD_MARKET` and are exempt
from the per-sector cap, because they are diversified by construction.

## 2. The three strategies

Defaults in [`configs/us_equities/portfolio.yaml`](../../configs/us_equities/portfolio.yaml);
every number is configuration, not advice.

| | conservative | balanced | aggressive |
| --- | --- | --- | --- |
| cash | 10% | 5% | 3% |
| bonds | 40% | 20% | 5% |
| index core (S&P 500) | 35% | 35% | 27% |
| quality single names | 15% | 25% | 30% |
| growth satellites | 0% | 13% | 25% |
| speculative | 0% | 2% | 10% |
| max per name | 4% | 6% | 10% |
| max per sector | 15% | 25% | 35% |
| max names per sleeve | 6 | 8 | 10 |
| inverse-vol tilt | 0.70 | 0.50 | 0.25 |
| risk buckets admitted | low | low, moderate | low, moderate, high |
| speculative enabled | no | yes | yes |

Reading the ladder: conservative keeps half the portfolio in cash and bonds
and holds only a few low-volatility names; balanced is a classic
core-satellite split; aggressive shrinks the stability sleeves, trebles the
growth satellites and opens a 10% speculative sleeve. Risk rises
monotonically in equity exposure, speculative exposure and per-name
concentration — asserted by a test.

## 3. Weighting inside a sleeve

For a sleeve with budget `B` and members `i`:

```
s_i    = max(allocation_adjusted_score_i, 1.0)
v_i    = volatility_pct_i           (annualized fraction; sleeve median if missing)
raw_i  = s_i ** (1 - tilt)  *  v_i ** (-tilt)
w_i    = B * raw_i / Σ raw
```

* `tilt = 0` → pure score weighting. `tilt = 1` → pure inverse-volatility
  (risk-parity-lite). The geometric blend keeps both factors strictly
  positive and makes the two extremes exact (tested).
* `allocation_adjusted_score` already embeds fundamentals, growth, quality,
  valuation and **confidence-adjusted news sentiment**, minus risk penalties.
  Allocation deliberately does **not** re-score anything; it only sizes.
* A missing volatility falls back to the **sleeve median**, which is recorded,
  not silently treated as zero risk.
* Members are the top `max_names_per_sleeve` by score, tie-broken by ticker so
  the plan is deterministic.

## 4. Concentration and business-type diversification

Two caps, applied in order:

1. **Per-name cap** (inside each sleeve). A position above
   `max_weight_per_name` is clamped and the excess is redistributed
   proportionally to the sleeve's remaining headroom, iterating until the
   sleeve is balanced or saturated.
2. **Per-sector cap** (portfolio-wide, across every single name — this is the
   "several different business types" requirement). An over-cap sector is
   scaled down pro-rata and the freed weight is re-offered to positions in the
   same sleeve whose sector still has room and which are below the per-name
   cap, so the strategy's sleeve budget is preserved.

Whatever still cannot be placed **overflows to `overflow_sleeve`** (the broad
index core, so it is always placeable) and is reported. Nothing is silently
dropped; each path raises a named warning:

| Warning | Meaning |
| --- | --- |
| `EMPTY_SLEEVE:<sleeve>` | no eligible names; the whole budget was redirected |
| `SLEEVE_SATURATED:<sleeve>` | too few names to absorb the budget under the per-name cap |
| `SECTOR_CAP_OVERFLOW` | a sector breached its cap and the excess was redirected |
| `PRICE_UNAVAILABLE:<ticker>` | dollar target produced, but no share count |
| `POSITIONS_NOT_INVESTABLE` | some targets were too small, or the share price too high |

Configuration validation rejects an infeasible design up front: a sleeve
budget larger than `max_names_per_sleeve × max_weight_per_name` could never be
filled, and `max_weight_per_name > max_weight_per_sector` would make a single
name unable to satisfy the sector cap.

## 5. Eligibility

A name may be held only if selection marked it `eligible_for_allocation`,
its `risk_bucket` is admitted by the strategy, and it has a positive
`last_close` (no price, no share count).

The speculative sleeve is the deliberate exception: selection marks those
names `exclude_from_allocation_by_default`, so they are *not*
`eligible_for_allocation` and are admitted **only** when the strategy sets
`allow_speculative`, and **only** into the speculative sleeve. Every such
position carries `SPECULATIVE_RESEARCH_ONLY`.

## 6. Money

```
target_value = weight × investable_amount
shares       = floor( target_value / (price × (1 + commission_pct)) )
invested     = shares × price
commission   = invested × commission_pct
cash         = cash_budget + (investable − cash_budget − invested − commission)
```

Flooring against the **commission-inclusive** unit cost guarantees a plan
never overspends its capital; money is conserved as
`invested + commission + cash == investable` (tested). `--fractional-shares`
skips the floor and places the money far more precisely.

Honest edge cases: a target below `min_position_value` is reported
`BELOW_MIN_POSITION_VALUE`, and a share price above the whole target is
reported `PRICE_EXCEEDS_TARGET_VALUE` — in both cases the money stays in cash
and the report says so, rather than emitting a zero-share row.

## 7. Trust gate

The allocator reads the selection run's own verdict from
`asset_selection_summary.json`:

| Selection `run_status` | Allocation behavior |
| --- | --- |
| `VALID` | plans built, status `VALID` |
| `PARTIAL` | plans built, status `PARTIAL`, caveat recorded |
| `DIAGNOSTIC` / `INVALID` | **refused** (status `INVALID`, no plans) unless `--allow-untrusted-ranking`, which produces `DEGRADED` |
| missing / unreadable | plans built, status `DEGRADED` — trustworthiness unverified |

A degraded allocation never looks like a valid one: the status appears in the
report banner, the JSON and stdout.

## 8. Diagnostics

Per strategy: realized sleeve weights vs. budget, `equity_weight`,
`largest_position_weight`, sector weights, and `effective_positions`
(`1 / HHI` over weights **normalized to the invested book**, so it is bounded
by the holding count and is not inflated by unpriced sleeves).

## 9. Commands

```bash
# all three strategies for $25,000
trading-portfolio --config configs/us_equities/portfolio.yaml --amount 25000

# one strategy, fractional shares
trading-portfolio --amount 5000 --strategy aggressive --fractional-shares

# no network: supply the ETF prices yourself
trading-portfolio --amount 25000 --offline \
    --instrument-price SPLG=68.10 BND=73.40

# turn the speculative sleeve off (its budget is redirected to the index core)
trading-portfolio --amount 25000 --no-speculative
```

Outputs:

```
data/us_equities/processed/portfolio_{conservative,balanced,aggressive}.csv
data/us_equities/processed/portfolio_allocations.csv
reports/us_equities/portfolio/portfolio_allocation.md
reports/us_equities/portfolio/portfolio_summary.json
```

## 10. Limitations and what is deliberately absent

- **No optimizer.** Mean-variance, risk parity and Black-Litterman need a
  return-covariance estimate this repository does not yet produce honestly;
  they stay on the roadmap. The current construction is transparent and
  rule-based by choice.
- **No expected return, no forecast, no backtest.** The weights are not
  claimed to be optimal or profitable; no historical performance of these
  portfolios has been measured.
- **No rebalancing against existing holdings yet.** Today the output is a
  target plan from cash. Drift-and-order rebalancing (the PAP engine's
  `diff_pesos` pattern) is the next step.
- **No portfolio risk metrics yet** (portfolio volatility, VaR, max drawdown,
  correlation). Per-name volatility is used for the tilt, but a portfolio-level
  risk report needs a returns panel — `asset_selection.risk` remains a stub.
- Sector labels come from the fundamentals provider and are occasionally
  missing; such names group under `UNKNOWN` and still count toward a cap.
- The bond proxy is an equity-market ETF stand-in for fixed income, not an
  actual bond ladder or a local-currency instrument.
