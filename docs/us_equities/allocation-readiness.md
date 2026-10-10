# Allocation readiness review and architecture decision

Reviewed: this repository's `asset_selection` module (post-restructure) and
two prior university projects, to decide how Milestone 2 (portfolio
construction) should be built.

## 1. Is the selection module ready to feed an allocator? **Yes.**

The selection pipeline was built with this handoff in mind and already emits
every field a portfolio constructor needs — no new upstream work is required:

| Need | Field already produced |
| --- | --- |
| Which names may be held at all | `eligible_for_allocation`, `allocation_exclusion_reasons` |
| How attractive (fundamentals + news + risk, already combined) | `allocation_adjusted_score`, `final_score`, `fundamentals_score` |
| **Which sleeve a name belongs to** | `candidate_role` = `core_candidate` / `satellite_growth_candidate` / `speculative_research_only` / `watchlist_only` |
| Risk class | `risk_bucket` (low/moderate/high), `risk_penalty`, `volatility_pct` |
| Business-type diversification | `sector`, `industry` |
| Liquidity | `avg_dollar_volume`, `market_cap` |
| News quality | `sentiment_score`, `sentiment_confidence`, `sentiment_quality_bucket` |
| Money conversion | `last_close` |
| Trust gate | `run_status`, `ranking_validity`, `data_quality_bucket` |

`candidate_role` maps one-to-one onto the three sleeves this milestone needs:
index-like core, small growth satellites, and a hard-capped speculative
sleeve. `recommended_next_step` already returns
`eligible_for_portfolio_optimizer` for exactly the names an allocator should
consume, and `speculative_research_only` is flagged
`exclude_from_allocation_by_default` — so an opt-in is required to use it.

**Conclusion:** the gap is purely downstream. Nothing in selection needs to
change; the allocator consumes `asset_selection_results.csv`.

## 2. Reviewing the two prior projects

### A. `PAP/PAP_Portfolio_Management` — adopt this architecture ✅

A wealth-advisory analytics dashboard. The relevant parts:

- **Model portfolios per risk profile** (`carteras_modelo.csv`): five
  profiles (1 = conservative … 5 = aggressive), each a set of target weights
  over *asset-class sleeves*:

  | sleeve | profile 1 | 3 | 5 |
  | --- | --- | --- | --- |
  | cash | 5% | 5% | 5% |
  | fixed income (CETES) | 95% | 50% | 20% |
  | broad equity core (SPLG = S&P 500) | 0% | 32% | 54% |
  | global equity (ACWI) | 0% | 8% | 12% |
  | sector satellites (IXJ health, IUIT tech) | 0% | 5% | 9% |

- **Rebalancing engine** (`utils/Rebalanceo.py`): profile → model weights →
  merge with current holdings → `diff_pesos` drift → integer share orders
  (`np.trunc`) → BUY/SELL/HOLD → 0.20% commission → cash reconciliation →
  resulting actual weights.
- **Risk metrics** (`utils/analytics.py::perf_metrics`): annualized return,
  volatility (√252), Sharpe and Sortino vs. the risk-free rate, max
  drawdown, historical VaR95 and Expected Shortfall.

This is precisely the professional pattern the request describes — a
risk-profiled, core-satellite model portfolio with sleeve budgets, converted
to money and share counts with costs. **It becomes the template.**

### B. `Trading/004_DL/deep_learning` — borrow mechanics, reject methodology ⚠️

Binary buy/sell classifiers (DNN/CNN/RNN) over technical indicators, tuned
with Optuna, plus a backtest loop with cash, commission, `n_shares`, stop-loss
and take-profit.

Worth borrowing: the **cash/commission/integer-share accounting** discipline
and the explicit position-level risk parameters.

Deliberately **not** adopted, because it conflicts with this repository's
standards:

1. `train_test_split(..., random_state=42)` on time-series data — random
   shuffling leaks future information. This repo mandates chronological
   splits (see `docs/binary_options/evaluation.md`).
2. The Optuna objective maximizes **classification accuracy**, not
   risk-adjusted return — accuracy is close to meaningless for an imbalanced
   directional target and says nothing about profitability.
3. The models are unvalidated black boxes whose output would have to be
   presented as a probability — which this repo forbids without measured,
   out-of-sample calibration.
4. Position sizing is a fixed `n_shares`, not a portfolio-level weight, so
   it does not answer "what percentage of my capital".

Short-horizon directional research already has an honest home in
`binary_options_selection`. The DL classifiers are therefore **not** wired
into allocation; the speculative sleeve is sized by a capped budget, not by
an unvalidated forecast.

## 3. Chosen architecture

Build `asset_selection.allocation` (the existing roadmap placeholder),
consuming the selection CSV:

```
selection ranking (fundamentals + news + risk, already scored)
        │
        ├─ eligibility gate  (eligible_for_allocation, run_status)
        │
        ▼
sleeve assignment by candidate_role
        │  index_core      → broad-market ETF (the "cover the S&P 500" sleeve)
        │  equity_core     → core_candidate names
        │  growth_satellite→ satellite_growth_candidate names (small weights)
        │  speculative     → speculative_research_only names (hard cap, opt-in)
        │  bond_proxy/cash → stability sleeve
        ▼
three strategies = three sleeve budgets  (conservative / balanced / aggressive)
        │
        ▼
within-sleeve weighting: score-weighted × inverse-risk tilt
        │   then per-name cap and per-sector cap, iteratively redistributed
        ▼
money layer: investable amount → target $ → integer shares at last_close
             → commission → residual cash → realized weights
        ▼
outputs: CSV + Markdown (three strategies, % and $) + JSON
```

Key decisions:

- **Sleeve budgets live in YAML**, validated, one block per strategy — the
  PAP model-portfolio idea, but with the equity sleeves *filled from the
  selection ranking* instead of hard-coded tickers. That is the genuine link
  between part 1 and part 2.
- **Sector caps** implement "several different business types": no single
  sector may exceed a configured share of equity.
- **The speculative sleeve is opt-in and capped**, consistent with selection
  marking those names `exclude_from_allocation_by_default`.
- **No optimizer that needs a covariance matrix** in this first version.
  Mean-variance/Black-Litterman need a return-covariance estimate the repo
  does not yet produce honestly; they stay on the roadmap. Score-weighting
  with an inverse-volatility tilt and hard caps is transparent and
  explainable — consistent with the repo's explainability rule.
- **Research output only.** The allocator reports percentages and money for a
  user-supplied amount; it places no orders, and it is not advice.
