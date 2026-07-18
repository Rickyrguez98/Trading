"""Research-only historical evaluation for binary-options signals.

Never imported by the live pipeline. See docs/binary_options/evaluation.md.
"""
from .engine import (  # noqa: F401
    BASELINES,
    EvalRecord,
    EvaluationConfig,
    evaluate_with_baselines,
    replay,
)
from .metrics import compare_to_baselines, full_report, summarize  # noqa: F401
