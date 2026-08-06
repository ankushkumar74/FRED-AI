"""Confidence scoring — BLUEPRINT 12.4.

Geometric mean, deliberately: a single near-zero component (e.g. stale data)
must collapse the whole score. Arithmetic averaging would let a large effect
size mask bad data, which is precisely the failure mode we cannot have.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

WEIGHTS = {
    "statistical": 0.20,
    "effect": 0.20,
    "coverage": 0.20,
    "data_quality": 0.15,
    "sample": 0.10,
    "corroboration": 0.10,
    "replication": 0.05,
}

# Confidence band -> permitted narrative language. Enforced by the verifier;
# confidence that does not change the wording is decoration.
BANDS = [
    (0.85, "causal", ["caused by", "driven by"]),
    (0.65, "probable", ["primarily explained by", "the evidence points to"]),
    (0.45, "possible", ["may be related to", "one possible driver"]),
    (0.00, "insufficient", []),
]


@dataclass(frozen=True)
class ConfidenceInputs:
    p_value: float
    effect_size: float
    historical_volatility: float
    explained_share: float
    data_quality_score: float
    n_observations: int
    independent_confirmations: int = 0
    pattern_seen_before: bool = False


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def score_confidence(i: ConfidenceInputs) -> float:
    vol = max(i.historical_volatility, 1e-9)
    components = {
        "statistical": min(max(1 - i.p_value, 0.0), 1.0),
        "effect": _sigmoid(abs(i.effect_size) / vol),
        "coverage": min(max(i.explained_share, 0.0), 1.0),
        "data_quality": min(max(i.data_quality_score, 0.0), 1.0),
        "sample": _sigmoid((i.n_observations - 30) / 30),
        "corroboration": min(1.0, 0.6 + 0.2 * i.independent_confirmations),
        "replication": 1.0 if i.pattern_seen_before else 0.85,
    }
    log_sum = sum(WEIGHTS[k] * math.log(max(v, 1e-6)) for k, v in components.items())
    return round(math.exp(log_sum / sum(WEIGHTS.values())), 3)


def band(confidence: float) -> str:
    for threshold, name, _ in BANDS:
        if confidence >= threshold:
            return name
    return "insufficient"
