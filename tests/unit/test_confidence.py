"""Confidence scoring — BLUEPRINT 12.4.

The property under test is the choice of a weighted *geometric* mean. An
arithmetic mean lets one strong component carry a finding whose data is stale or
whose sample is tiny; a geometric mean cannot, because any near-zero factor
collapses the product. That is the whole reason for the choice, so it is what
these tests pin.
"""

from __future__ import annotations

import pytest

from services.evidence.confidence import ConfidenceInputs, band, score_confidence


def inputs(**overrides: object) -> ConfidenceInputs:
    base = {
        "p_value": 0.001,
        "effect_size": 0.25,
        "historical_volatility": 0.05,
        "explained_share": 0.9,
        "data_quality_score": 1.0,
        "n_observations": 120,
        "independent_confirmations": 2,
        "pattern_seen_before": True,
    }
    base.update(overrides)
    return ConfidenceInputs(**base)  # type: ignore[arg-type]


class TestScoring:
    def test_strong_evidence_scores_high(self) -> None:
        assert score_confidence(inputs()) > 0.85

    def test_score_is_bounded(self) -> None:
        assert 0.0 <= score_confidence(inputs()) <= 1.0

    def test_insignificant_result_scores_low(self) -> None:
        assert score_confidence(inputs(p_value=0.9)) < 0.5

    def test_effect_inside_normal_volatility_scores_low(self) -> None:
        """A 2% move in a metric that swings 20% weekly is not a finding."""
        weak = score_confidence(inputs(effect_size=0.02, historical_volatility=0.2))
        strong = score_confidence(inputs(effect_size=0.4, historical_volatility=0.02))
        assert weak < strong

    def test_deterministic(self) -> None:
        assert score_confidence(inputs()) == score_confidence(inputs())


class TestGeometricMeanCollapse:
    """One bad component must sink the score, not be averaged away."""

    @pytest.mark.parametrize(
        "field,value",
        [
            ("data_quality_score", 0.01),   # stale or incomplete data
            ("explained_share", 0.01),      # explains almost none of the movement
            ("p_value", 0.999),             # no statistical support
        ],
    )
    def test_single_weak_component_collapses_the_score(self, field: str, value: float) -> None:
        strong = score_confidence(inputs())
        crippled = score_confidence(inputs(**{field: value}))
        assert crippled < 0.5, f"{field}={value} should collapse the score, got {crippled}"
        assert crippled < strong / 2

    def test_stale_data_outweighs_a_large_effect(self) -> None:
        """The failure mode an arithmetic mean would allow: a dramatic number
        from a broken pipeline presented confidently."""
        assert score_confidence(inputs(effect_size=5.0, data_quality_score=0.02)) < 0.5

    def test_tiny_sample_lowers_confidence(self) -> None:
        assert score_confidence(inputs(n_observations=3)) < score_confidence(
            inputs(n_observations=200)
        )

    def test_corroboration_raises_confidence(self) -> None:
        assert score_confidence(inputs(independent_confirmations=0)) < score_confidence(
            inputs(independent_confirmations=3)
        )


class TestBands:
    """Confidence that does not change the wording is decoration."""

    @pytest.mark.parametrize(
        "score,expected",
        [
            (0.95, "causal"),
            (0.85, "causal"),
            (0.70, "probable"),
            (0.50, "possible"),
            (0.20, "insufficient"),
            (0.0, "insufficient"),
        ],
    )
    def test_band_boundaries(self, score: float, expected: str) -> None:
        assert band(score) == expected

    def test_bands_are_monotonic(self) -> None:
        order = ["insufficient", "possible", "probable", "causal"]
        seen = [band(s / 20) for s in range(21)]
        ranks = [order.index(b) for b in seen]
        assert ranks == sorted(ranks), "a higher score must never map to a weaker band"
