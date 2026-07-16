"""Horizon-aware directional scoring.

Exact formulas (also in BINARY_OPTIONS_METHODOLOGY.md). Given signed family
scores ``s_f ∈ [-1, +1]`` and the horizon group's configured weights ``w_f``
renormalized over the families actually available (missing families are
excluded — never neutral-filled):

    call_raw          = Σ_f w_f · max(s_f, 0)
    put_raw           = Σ_f w_f · max(−s_f, 0)
    call_score        = 100 · call_raw                        ∈ [0, 100]
    put_score         = 100 · put_raw                         ∈ [0, 100]
    directional_score = call_score − put_score                ∈ [−100, +100]
    contradiction     = min(call_raw, put_raw) / max(call_raw, put_raw)
                        (0 when both sides are 0)

    confidence_score  = (|directional_score| / 100)
                        · (1 − w_c · contradiction)          w_c = contradiction_penalty_weight
                        · (data_quality_score / 100)
                        · feature_coverage                    (available / total configured weight)
                        · vol_regime_factor                   ∈ [floor, 1]

``confidence_score`` is a bounded heuristic in [0, 1]. It is **not** a
calibrated probability and is never presented as one: no field of this module
is named ``probability`` and no expected value is derived from it.

CALL and PUT are symmetric by construction: negating every family score swaps
``call_score`` and ``put_score`` exactly and flips ``directional_score``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ..config import BinaryOptionsConfig, horizon_group
from ..features.engine import FeatureBundle


@dataclass
class DirectionalScores:
    horizon_minutes: int
    horizon_group: str
    call_score: float = 0.0
    put_score: float = 0.0
    directional_score: float = 0.0
    contradiction_ratio: float = 0.0
    confidence_score: float = 0.0
    feature_coverage: float = 0.0
    vol_regime_factor: float = 1.0
    data_quality_score: float = 0.0
    # family -> weighted signed contribution (in directional points, -100..100)
    contributions: Dict[str, float] = field(default_factory=dict)
    missing_families: Dict[str, str] = field(default_factory=dict)
    primary_drivers: List[str] = field(default_factory=list)
    primary_conflicts: List[str] = field(default_factory=list)


def score_direction(
    bundle: FeatureBundle,
    *,
    data_quality_score: float,
    cfg: BinaryOptionsConfig,
) -> DirectionalScores:
    group = horizon_group(bundle.horizon_minutes)
    configured = cfg.weights.for_group(group)
    total_configured = sum(w for w in configured.values() if w > 0)

    available: Dict[str, Tuple[float, float]] = {}  # family -> (weight, score)
    for family, weight in configured.items():
        if weight <= 0:
            continue
        if family in bundle.family_scores:
            available[family] = (weight, bundle.family_scores[family])

    out = DirectionalScores(
        horizon_minutes=bundle.horizon_minutes,
        horizon_group=group,
        data_quality_score=data_quality_score,
        vol_regime_factor=bundle.vol_regime_factor,
        missing_families=dict(bundle.missing_families),
    )
    available_weight = sum(w for w, _ in available.values())
    out.feature_coverage = (
        round(available_weight / total_configured, 4) if total_configured > 0 else 0.0
    )
    if not available:
        return out  # everything zero; the decision layer will emit NO_TRADE

    call_raw = 0.0
    put_raw = 0.0
    for family, (weight, s) in available.items():
        norm_w = weight / available_weight
        call_raw += norm_w * max(s, 0.0)
        put_raw += norm_w * max(-s, 0.0)
        out.contributions[family] = round(norm_w * s * 100.0, 2)

    out.call_score = round(100.0 * call_raw, 2)
    out.put_score = round(100.0 * put_raw, 2)
    out.directional_score = round(out.call_score - out.put_score, 2)
    peak = max(call_raw, put_raw)
    out.contradiction_ratio = round((min(call_raw, put_raw) / peak) if peak > 0 else 0.0, 4)

    w_c = cfg.signal_rules.contradiction_penalty_weight
    confidence = (
        (abs(out.directional_score) / 100.0)
        * (1.0 - w_c * out.contradiction_ratio)
        * (max(0.0, min(100.0, data_quality_score)) / 100.0)
        * out.feature_coverage
        * bundle.vol_regime_factor
    )
    out.confidence_score = round(max(0.0, min(1.0, confidence)), 4)

    # Drivers/conflicts: family contributions sorted by |points|, relative to
    # the winning direction. Deterministic tie-break by family name.
    direction = 1.0 if out.directional_score >= 0 else -1.0
    ranked = sorted(
        out.contributions.items(), key=lambda kv: (-abs(kv[1]), kv[0])
    )
    for family, points in ranked:
        label = f"{family} ({points:+.1f})"
        if points * direction > 0:
            out.primary_drivers.append(label)
        elif points * direction < 0:
            out.primary_conflicts.append(label)
    out.primary_drivers = out.primary_drivers[:3]
    out.primary_conflicts = out.primary_conflicts[:3]
    return out
