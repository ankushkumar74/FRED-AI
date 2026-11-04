"""Narrative verification — BLUEPRINT 9.2 stage 19, 12.5 defence 4-7.

Every numeric token in generated prose must trace to a cited Finding.
Two failures and we degrade to evidence cards with no prose: producing a
less impressive output is always better than producing a wrong one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

NUMBER = re.compile(r"[-+]?\$?\d[\d,]*\.?\d*\s?%?")
CAUSAL_VERBS = ("caused by", "driven by", "because of", "due to", "resulted from")


@dataclass
class VerificationResult:
    passed: bool
    unverified_numbers: list[str]
    ungrounded_causal_claims: list[str]
    hedging_violations: list[str]


def verify(narrative: str, findings: list[dict[str, Any]], tolerance: float = 0.005) -> VerificationResult:
    """TODO(phase-2): full implementation. See BLUEPRINT 12.5.

    1. extract every numeric token from the narrative
    2. assert each appears in a cited finding payload within `tolerance`
    3. assert causal verbs only attach to findings of causal type above band
    4. assert hedging language matches the confidence band (12.4 BANDS table)
    """
    raise NotImplementedError("Phase 2 — ship in the SAME sprint as narrative generation")
