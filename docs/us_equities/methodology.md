# US Equities — Methodology summary

> Full narrative with worked examples lives in the [module manual](README.md);
> this page is the compact formula reference.

## Pipeline (staged funnel)

Universe (~4 700 cleaned U.S. commons) → liquidity prescreen (top-K by
average dollar volume) → fundamentals prescreen (top-K by fundamentals
score) → news + sentiment for the shortlist only → composite + rank.

## Pillar scoring

Per pillar (growth, quality, valuation, balance sheet, cash flow): metrics
are winsorized (2%/98%), z-scored across the surviving universe, mapped to
0–100, and combined with YAML weights
([../../configs/us_equities/default.yaml](../../configs/us_equities/default.yaml)).
Missing fields are tracked per ticker and penalized
(`missing_penalty_per_field`), never imputed.

## Composite

```
final_score = w_fundamentals·fundamentals + w_growth·growth
            + w_quality·quality + w_valuation·valuation
            + w_sentiment·effective_sentiment − w_risk·risk_penalty
```

Default weights keep the fundamentals family at 0.85 vs sentiment 0.15, and
an output-validation check errors if sentiment could ever dominate
fundamentals.

## Sentiment (bounded by construction)

VADER default; optional FinBERT, comparison, and ensemble modes
([sentiment-models.md](sentiment-models.md)). The composite consumes
`effective_sentiment = neutral + confidence · (raw − neutral)` where
confidence needs many unique, fresh, source-diverse articles to approach 1
and is capped per model. **These scores rank multi-week attractiveness; they
are never predictors of intraday direction.**

## Run integrity

Benchmark provider health checks, coverage gating, materiality checks on
critical tickers, run statuses (`VALID`/`PARTIAL`/`DIAGNOSTIC`/`INVALID`)
with a banner on every report, and a post-run output self-audit. Details:
[architecture.md](architecture.md) and the historical [audits/](audits/).
