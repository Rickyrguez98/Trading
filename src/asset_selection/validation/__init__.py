"""Post-run output validation.

These checks run AFTER ranking and re-read the produced candidates the way a
skeptical reviewer would: did an excluded security type leak in, did providers
quietly fail, is sentiment confidence overstated, is a fundamentals score
propped up by a single pillar? The goal is to surface quality problems that a
"the pipeline finished" green light would otherwise hide.
"""
from .coverage import (
    COMPLETE,
    COMPLETE_WITH_MINOR_GAPS,
    DIAGNOSTIC_ONLY,
    INVALID_INSUFFICIENT_DATA,
    INVALID_PROVIDER_FAILURE,
    INVALID_SYSTEMIC_PROVIDER_FAILURE,
    PARTIAL_CRITICAL_TICKER_FAILURE,
    PARTIAL_RANKING_WITH_WARNINGS,
    VALID_RANKING,
    VALID_WITH_MATERIAL_WARNINGS,
    assess_coverage,
    assess_materiality,
    determine_run_status,
    is_trusted_run_status,
    return_code_for,
)
from .output_validation import (
    ValidationCheck,
    validate_outputs,
    write_validation_reports,
)
from .provider_diagnostics import (
    build_provider_diagnostics,
    build_provider_report,
    render_provider_provenance_note,
    render_run_status_banner,
    write_provider_diagnostics,
)

__all__ = [
    "ValidationCheck",
    "validate_outputs",
    "write_validation_reports",
    "assess_coverage",
    "assess_materiality",
    "determine_run_status",
    "is_trusted_run_status",
    "return_code_for",
    "VALID_RANKING",
    "PARTIAL_RANKING_WITH_WARNINGS",
    "DIAGNOSTIC_ONLY",
    "INVALID_PROVIDER_FAILURE",
    "INVALID_INSUFFICIENT_DATA",
    "COMPLETE",
    "COMPLETE_WITH_MINOR_GAPS",
    "VALID_WITH_MATERIAL_WARNINGS",
    "PARTIAL_CRITICAL_TICKER_FAILURE",
    "INVALID_SYSTEMIC_PROVIDER_FAILURE",
    "build_provider_diagnostics",
    "build_provider_report",
    "render_provider_provenance_note",
    "render_run_status_banner",
    "write_provider_diagnostics",
]
