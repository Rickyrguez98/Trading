"""Asset allocation — **future milestone**.

This package is intentionally a stub. The current asset-selection pipeline
does not import anything from here.

Planned (see ``docs/FUTURE_ROADMAP.md``):
    * equal-weight top-K
    * score-weighted with per-name and per-sector caps
    * risk parity
    * mean-variance optimization with shrinkage covariance
    * Black-Litterman with composite-score views

Each strategy will implement a ``BaseAllocator.allocate(ranked_df) -> weights``
interface so the pipeline can swap them via the YAML config.
"""

__all__: list[str] = []
