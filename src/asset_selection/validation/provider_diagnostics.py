"""Provider diagnostics report -> reports/provider_diagnostics.{json,md}.

This is the single artifact a human (or a downstream rebalancer) reads to answer
"can I trust today's ranking, and if not, why?" It consolidates, in one place:

  * the run-status verdict (VALID / PARTIAL / DIAGNOSTIC / INVALID) and why,
  * the benchmark health check (were the mega-caps fetchable?),
  * data-coverage ratios and the provider-side failure load,
  * the honest provider-failure breakdown by error taxonomy,
  * fallback-chain usage (how often a backup / stale cache was needed),
  * cache provenance (live vs fresh_cache vs stale_cache vs fallback).

Nothing here recomputes findings -- it renders what the pipeline already
measured. It is deliberately verbose on failure and terse on success.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Plain-language gloss for each error-taxonomy constant, so the report reads
# like an explanation rather than a list of enum names.
_ERROR_GLOSS = {
    "INVALID_TICKER": "symbol is malformed / not a real ticker",
    "UNSUPPORTED_PROVIDER_SYMBOL": "provider can't express this symbol",
    "NO_PRICE_DATA": "no price history returned (illiquid/new/uncovered)",
    "NO_FUNDAMENTAL_DATA": "no fundamentals returned",
    "NO_NEWS_DATA": "no recent news (not a failure on its own)",
    "PROVIDER_EMPTY_RESPONSE": "provider returned an empty payload",
    "POSSIBLY_DELISTED": "no data after normalization; may be delisted",
    "PROVIDER_RATE_LIMITED": "provider rate-limited the request",
    "PROVIDER_TIMEOUT": "request timed out",
    "PROVIDER_BLOCKED": "provider blocked the request (auth/forbidden)",
    "PROVIDER_JSON_PARSE_ERROR": "got HTML/garbage instead of JSON (often blocked)",
    "PROVIDER_HTTP_ERROR": "provider returned an HTTP error",
    "PROVIDER_UNKNOWN_ERROR": "unclassified provider error",
}


_DATA_TYPES = ("prices", "fundamentals", "news")
# Which staged fetch produces each data type's provenance, for cache_usage_by_stage.
_STAGE_BY_DATA_TYPE = {"prices": "2_prices", "fundamentals": "3_fundamentals"}


def build_provider_report(
    *,
    configured_providers: Dict[str, Any],
    fallback_usage: Dict[str, Any],
    cache_usage: Dict[str, Any],
) -> Dict[str, Any]:
    """One consistent provider-provenance block, shared by every artifact.

    The same dict is embedded in ``asset_selection_summary.json``,
    ``provider_diagnostics.{json,md}`` and the top-candidates banner, so the four
    reports can never disagree about which provider was configured, what the
    fallback chain was, or how often a backup actually fired.

    It also fixes the *misleading-zero* problem: a single (unwrapped) provider is
    not instrumented for primary/fallback counters, so instead of printing
    ``primary=0, fallback=0`` -- which reads as "served nothing" -- it reports
    ``instrumented: false`` and falls back to the cache/provenance counts, which
    *are* measured for every record.
    """
    configured = dict(configured_providers or {})
    fb_all = fallback_usage if isinstance(fallback_usage, dict) else {}
    cu_all = cache_usage if isinstance(cache_usage, dict) else {}

    chain_by_dt: Dict[str, List[str]] = {}
    actual_usage: Dict[str, Any] = {}
    for dt in _DATA_TYPES:
        fb = fb_all.get(dt) or {}
        chain = list(fb.get("chain") or [])
        if not chain and configured.get(dt):
            chain = [configured[dt]]
        chain_by_dt[dt] = chain

        wrapped = bool(fb.get("wrapped"))
        by_source = dict(cu_all.get(dt) or {})  # data_source provenance counts
        entry: Dict[str, Any] = {
            "configured": configured.get(dt),
            "chain": chain,
            "instrumented": wrapped,
            "by_source": by_source,
        }
        if wrapped:
            entry["by_provider"] = dict(fb.get("by_provider") or {})
            entry["counters"] = {
                "primary": fb.get("primary", 0),
                "fallback": fb.get("fallback", 0),
                "stale_cache": fb.get("stale_cache", 0),
                "unavailable": fb.get("unavailable", 0),
            }
        actual_usage[dt] = entry

    cache_by_stage = {
        stage: dict(cu_all.get(dt) or {})
        for dt, stage in _STAGE_BY_DATA_TYPE.items()
    }
    return {
        "configured_providers": configured,
        "provider_chain_by_data_type": chain_by_dt,
        "actual_provider_usage": actual_usage,
        "cache_usage_by_stage": cache_by_stage,
    }


def build_provider_diagnostics(
    *,
    status: Dict[str, Any],
    coverage: Dict[str, Any],
    health_report: Optional[Dict[str, Any]],
    provider_failures: Dict[str, Any],
    fallback_usage: Dict[str, Any],
    cache_usage: Dict[str, Any],
    providers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the diagnostics dict written to reports/provider_diagnostics.json."""
    provider_report = build_provider_report(
        configured_providers=providers or {},
        fallback_usage=fallback_usage or {},
        cache_usage=cache_usage or {},
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_status": status.get("run_status"),
        "ranking_validity": status.get("ranking_validity"),
        "is_trusted": status.get("is_trusted"),
        "invalid_ranking_reasons": list(status.get("invalid_ranking_reasons", [])),
        "warnings": list(status.get("warnings", [])),
        "recommendations_for_next_run": list(
            status.get("recommendations_for_next_run", [])
        ),
        "configured_providers": providers or {},
        "provider_report": provider_report,
        "provider_health_check_summary": health_report or {},
        "data_coverage_summary": coverage or {},
        "provider_failure_summary": provider_failures or {},
        "fallback_usage_summary": fallback_usage or {},
        "cache_usage_summary": cache_usage or {},
    }


def render_provider_provenance_note(provider_report: Dict[str, Any]) -> str:
    """A compact provider-provenance footer appended to top_candidates.md so the
    headline report names the same providers/chain as the diagnostics."""
    if not provider_report:
        return ""
    configured = provider_report.get("configured_providers") or {}
    chains = provider_report.get("provider_chain_by_data_type") or {}
    lines: List[str] = ["## Data providers", ""]
    lines.append("| Data type | Configured | Fallback chain |")
    lines.append("| --- | --- | --- |")
    for dt in _DATA_TYPES:
        chain = chains.get(dt) or ([configured.get(dt)] if configured.get(dt) else [])
        chain_str = " → ".join(str(c) for c in chain) if chain else "—"
        lines.append(f"| {dt} | {configured.get(dt) or '—'} | {chain_str} |")
    lines += ["",
              "_See `provider_diagnostics.md` for actual per-provider usage, cache "
              "provenance, and the run-status verdict._", ""]
    return "\n".join(lines) + "\n"


def write_provider_diagnostics(
    diag: Dict[str, Any], output_dir: Path
) -> "tuple[Path, Path]":
    """Write provider_diagnostics.{json,md}; return their paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "provider_diagnostics.json"
    md_path = output_dir / "provider_diagnostics.md"
    json_path.write_text(json.dumps(diag, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_render_markdown(diag), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_run_status_banner(
    status: Dict[str, Any], coverage: Optional[Dict[str, Any]] = None
) -> str:
    """A compact banner prepended to top_candidates.md so the headline report
    can never be read without its validity caveat."""
    rs = status.get("run_status", "UNKNOWN")
    rv = status.get("ranking_validity", "UNKNOWN")
    trusted = status.get("is_trusted", False)
    lines: List[str] = []
    if trusted and rs == "VALID":
        lines.append(f"> **Run status: {rs}** (ranking_validity: {rv}).")
        lines.append("> Data coverage met all configured thresholds.")
    elif trusted:  # PARTIAL
        lines.append(f"> **Run status: {rs} — read with caution** "
                     f"(ranking_validity: {rv}).")
        lines.append("> Coverage is degraded; this is a partial ranking with "
                     "warnings, not a clean result.")
    else:  # DIAGNOSTIC / INVALID
        lines.append(f"> **Run status: {rs} — NOT A TRUSTED RANKING** "
                     f"(ranking_validity: {rv}).")
        lines.append("> The data was insufficient or a provider failed "
                     "systemically. Treat the table below as diagnostics only.")
    for r in status.get("invalid_ranking_reasons", []) or []:
        lines.append(f"> - {r}")
    recs = status.get("recommendations_for_next_run", []) or []
    if recs:
        lines.append(">")
        lines.append("> _Next run:_ " + " ".join(recs))
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_markdown(diag: Dict[str, Any]) -> str:
    rs = diag.get("run_status", "UNKNOWN")
    rv = diag.get("ranking_validity", "UNKNOWN")
    trusted = diag.get("is_trusted", False)
    lines: List[str] = [
        "# Provider Diagnostics",
        "",
        f"_Generated: {diag.get('generated_at')}_",
        "",
        "> Research output only. Not financial advice. See `docs/DISCLAIMER.md`.",
        "",
        f"## Run status: {rs}",
        "",
        f"- **ranking_validity:** `{rv}`",
        f"- **trusted ranking:** {'yes' if trusted else 'NO'}",
        "",
    ]

    reasons = diag.get("invalid_ranking_reasons") or []
    if reasons:
        lines += ["### Why this run is not a clean ranking", ""]
        lines += [f"- {r}" for r in reasons]
        lines.append("")
    warnings = diag.get("warnings") or []
    if warnings:
        lines += ["### Warnings", ""]
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    recs = diag.get("recommendations_for_next_run") or []
    if recs:
        lines += ["### Recommendations for next run", ""]
        lines += [f"- {r}" for r in recs]
        lines.append("")

    # --- Benchmark health ---
    health = diag.get("provider_health_check_summary") or {}
    if health:
        lines += ["## Benchmark health check", "",
                  f"- overall: `{health.get('overall_status')}`",
                  f"- benchmark tickers: {health.get('benchmark_tickers')}",
                  f"- price systemic failure: {health.get('price_systemic_failure')}",
                  f"- fundamentals systemic failure: "
                  f"{health.get('fundamentals_systemic_failure')}",
                  ""]
        by_dt = health.get("by_data_type") or {}
        if by_dt:
            lines += ["| Data type | Provider | Checked | Succeeded | Systemic |",
                      "| --- | --- | --- | --- | --- |"]
            for dt, blk in by_dt.items():
                lines.append(
                    f"| {dt} | {blk.get('provider_name')} | {blk.get('checked')} "
                    f"| {blk.get('succeeded')} | {blk.get('systemic_failure')} |"
                )
            lines.append("")

    # --- Coverage ---
    cov = diag.get("data_coverage_summary") or {}
    if cov:
        lines += ["## Data coverage", "",
                  "| Data type | Coverage | Attempted | With data | Meets threshold |",
                  "| --- | --- | --- | --- | --- |"]
        for dt in ("price", "fundamentals", "news"):
            blk = cov.get(dt) or {}
            meets = cov.get(f"meets_{'fundamentals' if dt=='fundamentals' else dt}_coverage")
            lines.append(
                f"| {dt} | {blk.get('coverage_ratio')} | {blk.get('attempted')} "
                f"| {blk.get('with_data')} | {meets} |"
            )
        lines += ["",
                  f"- provider-side failure ratio: {cov.get('provider_failure_ratio')} "
                  f"(budget {cov.get('thresholds', {}).get('max_provider_failure_ratio')})",
                  f"- valid candidates (real fundamentals): {cov.get('valid_candidates')} "
                  f"/ {cov.get('ranked_candidates')} ranked",
                  ""]

    # --- Provider failures by error taxonomy ---
    pf = diag.get("provider_failure_summary") or {}
    by_err = pf.get("by_error_type") or {}
    if pf.get("total"):
        lines += ["## Provider failures (honest error taxonomy)", "",
                  f"Total non-usable responses: **{pf.get('total')}** "
                  f"(provider-side: {pf.get('provider_side_failures', 0)}).", "",
                  "| Error type | Count | Meaning |", "| --- | --- | --- |"]
        for et, n in sorted(by_err.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{et}` | {n} | {_ERROR_GLOSS.get(et, '')} |")
        lines.append("")

    # --- Configured providers + fallback chain (consistent provenance block) ---
    report = diag.get("provider_report") or {}
    configured = report.get("configured_providers") or diag.get("configured_providers") or {}
    chains = report.get("provider_chain_by_data_type") or {}
    actual = report.get("actual_provider_usage") or {}
    if configured or chains:
        lines += ["## Providers (configured + chain)", "",
                  "| Data type | Configured | Fallback chain |",
                  "| --- | --- | --- |"]
        for dt in _DATA_TYPES:
            chain = chains.get(dt) or ([configured.get(dt)] if configured.get(dt) else [])
            chain_str = " → ".join(str(c) for c in chain) if chain else "—"
            lines.append(f"| {dt} | {configured.get(dt) or '—'} | {chain_str} |")
        lines.append("")

    # --- Actual provider usage ---
    # A single (unwrapped) provider is NOT instrumented for primary/fallback
    # counters, so we print "—" rather than a misleading 0 and lean on the
    # cache/provenance counts, which are measured for every record.
    if actual:
        lines += ["## Actual provider usage", "",
                  "| Data type | Instrumented | Primary | Fallback | Stale cache | Unavailable | By source |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for dt in _DATA_TYPES:
            blk = actual.get(dt) or {}
            instrumented = bool(blk.get("instrumented"))
            counters = blk.get("counters") or {}
            by_source = blk.get("by_source") or {}
            src = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "—"
            if instrumented:
                cells = (
                    f"{counters.get('primary', 0)} | {counters.get('fallback', 0)} "
                    f"| {counters.get('stale_cache', 0)} | {counters.get('unavailable', 0)}"
                )
            else:
                # Not instrumented: do not imply "served zero records".
                cells = "— | — | — | —"
            lines.append(f"| {dt} | {'yes' if instrumented else 'no'} | {cells} | {src} |")
        lines += ["",
                  "_\"Instrumented: no\" means a single provider with no fallback "
                  "wrapper; its activity is reflected in the by-source column and the "
                  "cache provenance below, not in the primary/fallback counters._", ""]

    # --- Cache provenance ---
    cu = diag.get("cache_usage_summary") or {}
    if cu:
        lines += ["## Cache provenance (data_source)", "",
                  "Where each fetched record came from. `stale_cache` means a "
                  "recent-but-expired entry was knowingly served after live "
                  "providers failed; it is never passed off as live.", ""]
        for dt, counts in cu.items():
            if not counts:
                continue
            pretty = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            lines.append(f"- **{dt}:** {pretty}")
        lines.append("")

    return "\n".join(lines) + "\n"
