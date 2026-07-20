# Documentation index

## Shared
- [Disclaimer](shared/disclaimer.md) — research-only scope; read first.
- [Engineering standards](shared/engineering-standards.md) — domain boundaries, shared-infrastructure rules, honesty rules, testing/CI policy.
- [Migration guide](shared/migration.md) — legacy → domain-separated paths, renamed groups, deprecations.
- [Architecture audit](shared/architecture-audit.md) — the pre-restructure baseline and migration plan.
- [Git authorship](shared/git-authorship.md) — repository identity policy and history notes.

## US Equities (`asset_selection`)
- [Module manual](us_equities/README.md) — full usage guide (modes, flags, outputs, interpretation).
- [Architecture](us_equities/architecture.md)
- [Methodology](us_equities/methodology.md) — pillar scoring, composite weights, sentiment bounds.
- [Data sources](us_equities/data-sources.md)
- [Outputs](us_equities/outputs.md) — file locations and schemas.
- [Sentiment models](us_equities/sentiment-models.md) — VADER / FinBERT / comparison / ensemble.
- [Full-universe pipeline](us_equities/full-universe-pipeline.md)
- [Roadmap](us_equities/roadmap.md)
- Historical audits: [audits/](us_equities/audits/)

## Binary Options (`binary_options_selection`)
- [Module overview](binary_options/README.md) — scope, quick start, honest defaults.
- [Architecture](binary_options/architecture.md)
- [Methodology](binary_options/methodology.md) — features, scoring formulas, thresholds, NO_TRADE gates.
- [Instrument catalog](binary_options/instrument-catalog.md) — schema, validation, platform adapter interface.
- [Signal interpretation](binary_options/signal-interpretation.md) — CALL / PUT / NO_TRADE, recommended_action, what confidence is and is not.
- [Data sources](binary_options/data-sources.md) — intraday providers, delay, caching.
- [Evaluation](binary_options/evaluation.md) — historical replay, splits, baselines, leakage prevention.
- [Limitations](binary_options/limitations.md)
- [Roadmap](binary_options/roadmap.md)
