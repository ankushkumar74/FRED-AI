"""Contribution and mix-shift decomposition — BLUEPRINT 12.3.

The single most valuable algorithm in the product. Separating *rate effect*
from *mix effect* is what distinguishes "conversion got worse" from
"conversion looks worse because traffic shifted to a low-converting channel."
Those two facts imply opposite actions.

TODO(phase-2): implement. See BLUEPRINT 12.3 for the formulas and the
Adtributor-style bounded multi-dimensional search.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentContribution:
    slice: dict[str, str]
    contribution_pp: float      # percentage points of the total movement
    rate_effect: float          # ratio metrics only
    mix_effect: float           # ratio metrics only
    joint_effect: float


@dataclass(frozen=True)
class Decomposition:
    total_change: float
    segments: list[SegmentContribution]
    unexplained_share: float    # ALWAYS reported. Analysts trust residuals.


def decompose_additive(*args: object, **kwargs: object) -> Decomposition:
    raise NotImplementedError("Phase 2 — BLUEPRINT 12.3")


def decompose_ratio(*args: object, **kwargs: object) -> Decomposition:
    raise NotImplementedError("Phase 2 — BLUEPRINT 12.3")
