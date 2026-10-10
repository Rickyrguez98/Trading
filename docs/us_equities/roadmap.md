# Future Roadmap

> This roadmap covers **Module 1 (equity asset selection)**. The binary-options
> module's roadmap (signal persistence, walk-forward evaluation, probability
> calibration, payout modeling) is separate:
> [`../binary_options/roadmap.md`](../binary_options/roadmap.md).

Milestone 1 (this repo) ends at producing a ranked candidate list. The next
milestones are sketched here so the asset-selection layer can be extended
without rewriting it. Each future module already has a placeholder package
under `src/asset_selection/` with a module-level docstring describing intent.

## Milestone 2 — Asset Allocation — **first version implemented**
**Package:** `asset_selection.allocation` · see [allocation.md](allocation.md)

Implemented: three risk strategies (conservative / balanced / aggressive) as
sleeve budgets over cash, bonds, an S&P 500 index core, quality single names,
growth satellites and an opt-in capped speculative sleeve; score-weighting with
an inverse-volatility tilt; per-name and portfolio-wide per-sector caps with
reported overflow; whole-share money conversion with commissions; a trust gate
on the selection run; CSV / Markdown / JSON outputs in percentage and money.

Still open from the original plan:

Given the ranked candidates and a notional capital base, decide *how much* to
hold of each.

- **Equal-weight top-K** — naive baseline, surprisingly hard to beat.
- ~~**Score-weighted** — weights ∝ composite score, capped per-name.~~ *(done)*
- **Risk parity** — equal risk contribution using historical covariance.
- **Mean-variance optimization (MVO)** — with shrinkage covariance.
- **Black-Litterman** — incorporate the composite score as a "view".

Constraints: per-name cap *(done)*, sector cap *(done)*, cash buffer *(done)*,
turnover budget *(open — needs drift-based rebalancing against existing
holdings)*.

Deferred deliberately: mean-variance, risk parity and Black-Litterman all need
a return-covariance estimate this repository does not yet produce honestly.

## Milestone 3 — Risk Management
**Package:** `asset_selection.risk`

- Per-name: realized vol, downside vol, max drawdown, beta vs SPY.
- Portfolio: correlation matrix (shrunk), VaR / CVaR, sector exposures,
  factor exposures (Fama–French if data available).
- Hard stops and soft alerts surfaced in reports.

## Milestone 4 — Backtesting
**Package:** `asset_selection.backtesting`

- Walk-forward replay of the selection rule with realistic rebalance cadence
  (monthly / quarterly).
- Out-of-sample only; explicit guards against look-ahead bias when reading
  fundamentals.
- Metrics: CAGR, Sharpe, Sortino, max DD, turnover, hit rate.

## Milestone 5 — Rebalancing & Transaction Costs
- Inertia/turnover thresholds: don't trade if delta < threshold.
- Slippage model: linear in % ADV.
- Commission model: per-share or per-trade.
- Tax-aware rebalancing (lot-level) as an optional add-on.

## Milestone 6 — Live data plumbing (still no execution)
- Scheduled pipeline runs (cron / systemd / Airflow).
- Diff reports vs previous run.
- Alerting on rank changes for held names.

> Execution / live trading is intentionally not on this roadmap. A separate
> repository would handle order management with the appropriate controls.
