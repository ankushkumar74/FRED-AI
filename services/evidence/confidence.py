"""Confidence scoring — BLUEPRINT 12.4.

Two kinds of input, combined differently on purpose:

- **Evidence strength** (effect size, coverage, sample, corroboration) —
  weighted geometric mean. A weak component drags the score down.
- **Admissibility** (statistical significance, data quality) — multipliers.
  A stale sync or an insignificant result vetoes the finding outright,
  however large the effect looks.

Everything was weighted components at first. That was wrong: at weight 0.15 a
data_quality of 0.02 still scored 0.54, comfortably inside the band the
narrative layer is allowed to call a probable cause. A weighted geometric mean
only collapses when the weak term carries most of the weight, so an input that
must be able to veto cannot be expressed as one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Evidence components: how *strong* the finding is. Combined by weighted
# geometric mean, so a weak component drags but does not by itself veto.
WEIGHTS = {
    "effect": 0.40,
    "coverage": 0.30,
    "sample": 0.15,
    "corroboration": 0.10,
    "replication": 0.05,
}

# Preconditions: whether the finding is *admissible at all*. Applied as
# multipliers rather than weighted terms.
#
# These were originally weighted components, and that was wrong. At weight 0.15 a
# data_quality of 0.02 — a sync three days stale — still produced an overall 0.54,
# high enough to be narrated as a probable cause. A weighted geometric mean only
# collapses when the weak term carries most of the weight, so a component that
# must be able to veto cannot be expressed as a weight. Statistical significance
# behaves the same way: p = 0.9 is not a weak finding, it is not a finding.
PRECONDITIONS = ("statistical", "data_quality")

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


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(value, low), high)


def score_confidence(i: ConfidenceInputs) -> float:
    """Combine evidence strength with admissibility preconditions.

        score = weighted_geometric_mean(evidence) x statistical x data_quality

    The multiplicative preconditions are what make a stale sync or an
    insignificant result unable to produce a confident claim, however large the
    effect looks.
    """
    vol = max(i.historical_volatility, 1e-9)

    evidence = {
        "effect": _sigmoid(abs(i.effect_size) / vol),
        "coverage": _clamp(i.explained_share),
        "sample": _sigmoid((i.n_observations - 30) / 30),
        "corroboration": min(1.0, 0.6 + 0.2 * i.independent_confirmations),
        "replication": 1.0 if i.pattern_seen_before else 0.85,
    }
    log_sum = sum(WEIGHTS[k] * math.log(max(v, 1e-6)) for k, v in evidence.items())
    strength = math.exp(log_sum / sum(WEIGHTS.values()))

    statistical = _clamp(1 - i.p_value)
    data_quality = _clamp(i.data_quality_score)

    return round(strength * statistical * data_quality, 3)


def band(confidence: float) -> str:
    for threshold, name, _ in BANDS:
        if confidence >= threshold:
            return name
    return "insufficient"
