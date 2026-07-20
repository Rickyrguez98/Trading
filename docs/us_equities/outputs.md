# US Equities — Output locations and schemas

| File | Content |
|---|---|
| `data/us_equities/processed/universe_full.csv` | raw stage-1 universe (pre-clean) |
| `data/us_equities/processed/universe_clean.csv` | cleaned common-stock universe |
| `data/us_equities/processed/asset_selection_results.csv` | full ranked table with every metric, flag, and eligibility field |
| `reports/us_equities/top_candidates.md` | human top-N report, prefixed with the run-status banner |
| `reports/us_equities/asset_selection_summary.json` | machine-readable run summary: run_status, ranking_validity, stages, coverage, provider failures, candidates |
| `reports/us_equities/universe_summary.json` | stage stats + exchange breakdown (written even on aborts) |
| `reports/us_equities/provider_diagnostics.{md,json}` | per-provider outcomes: "can I trust today's ranking?" |
| `reports/us_equities/provider_health.json` | benchmark mega-cap health probe |
| `reports/us_equities/output_validation.{md,json}` | post-run self-audit (ok/warn/error checks) |

Key per-candidate fields: identity + sector, `market_cap`,
`avg_dollar_volume`, pillar scores, `fundamentals_score`,
`sentiment_score` (+ raw/effective/confidence and per-model columns in
comparison modes), `risk_penalty`, `final_score`, `rank`,
`selection_bucket`, `eligible_for_allocation`,
`allocation_adjusted_score`, `warning_flags`, `missing_fields`, `reason`.

Interpretation guidance (research ranking vs allocation shortlist, flags,
run statuses) is in the [module manual](README.md).
