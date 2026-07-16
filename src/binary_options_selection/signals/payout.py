"""Binary-contract payout context — break-even math only.

For a binary contract paying a net ``payout_ratio`` r per unit staked on a win
(total return = 1 + r) and losing the stake otherwise, the break-even win
probability is where expected value is zero:

    p* · r − (1 − p*) = 0   ⇒   p* = 1 / (1 + r)

e.g. r = 0.85 ⇒ p* ≈ 52.6%. This is reported as **context only**: the module's
``confidence_score`` is a heuristic, not a calibrated probability, so
profitability cannot be inferred from it — and this module refuses to try.

``expected_edge`` exists precisely to enforce that refusal: it returns a value
only when given a genuinely calibrated probability, which nothing in this
milestone produces.
"""
from __future__ import annotations

from typing import Optional


def break_even_win_probability(payout_ratio: float) -> float:
    """p* = 1 / (1 + payout_ratio). Raises on non-positive ratios."""
    r = float(payout_ratio)
    if r <= 0:
        raise ValueError(f"payout_ratio must be positive, got {r}")
    return 1.0 / (1.0 + r)


def expected_edge(
    calibrated_probability: Optional[float],
    payout_ratio: float,
) -> Optional[float]:
    """Expected value per unit staked: p·r − (1 − p).

    Returns ``None`` when ``calibrated_probability`` is ``None`` — which is
    always the case in this milestone, because heuristic confidence scores
    must never be passed here. Do NOT feed ``confidence_score`` into this
    function; it is not a probability and any "edge" computed from it would
    be fiction.
    """
    if calibrated_probability is None:
        return None
    p = float(calibrated_probability)
    if not (0.0 <= p <= 1.0):
        raise ValueError(f"probability must be within [0, 1], got {p}")
    r = float(payout_ratio)
    if r <= 0:
        raise ValueError(f"payout_ratio must be positive, got {r}")
    return p * r - (1.0 - p)
