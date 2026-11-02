"""Resolver and loader behaviour.

The resolver tests are mostly about *restraint*: the valuable behaviour is
declining to answer, because a confident wrong metric is how a bad number
reaches a board deck.
"""

from __future__ import annotations

import pytest

from services.semantic.loader import SemanticLoadError, load_dict
from services.semantic.resolver import MetricResolver

MODEL = {
    "entities": [
        {
            "key": "orders",
            "dataset": "fct_orders",
            "default_time_column": "ordered_at",
            "dimensions": [
                {"key": "channel", "synonyms": ["source", "marketing channel"]},
                {"key": "region", "synonyms": ["geo", "market"]},
            ],
        },
        {
            "key": "spend",
            "dataset": "fct_spend",
            "default_time_column": "spend_date",
            "dimensions": [{"key": "channel"}, {"key": "campaign"}],
        },
    ],
    "metrics": [
        {"key": "gross_revenue", "entity": "orders", "agg": "sum", "sql": "{{entity}}.total",
         "synonyms": ["revenue", "sales", "GMV"], "certified": True},
        {"key": "refunds", "entity": "orders", "agg": "sum", "sql": "{{entity}}.refund",
         "synonyms": ["returns"], "certified": True},
        {"key": "order_count", "entity": "orders", "agg": "count", "sql": "{{entity}}.id",
         "synonyms": ["orders", "number of orders"], "certified": True},
        {"key": "marketing_spend", "entity": "spend", "agg": "sum", "sql": "{{entity}}.spend",
         "synonyms": ["ad spend", "media spend"], "certified": True},
        {"key": "cac", "type": "ratio", "numerator": "marketing_spend", "denominator": "order_count",
         "synonyms": ["customer acquisition cost", "cost per customer", "CPA"], "certified": True},
        {"key": "ctr", "type": "ratio", "numerator": "order_count", "denominator": "order_count",
         "synonyms": ["click through rate", "click rate"]},
    ],
}


@pytest.fixture
def resolver() -> MetricResolver:
    return MetricResolver(load_dict(MODEL))


class TestBinding:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("what was revenue last month", "gross_revenue"),
            ("GMV by region", "gross_revenue"),
            ("how many orders", "order_count"),
            ("ad spend by channel", "marketing_spend"),
            ("how much did we spend on marketing", "marketing_spend"),
            ("cost per customer by channel", "cac"),
            ("CPA trend", "cac"),
            ("how much did we refund in July", "refunds"),
            ("returns last week", "refunds"),
        ],
    )
    def test_binds_the_right_metric(self, resolver: MetricResolver, question: str, expected: str) -> None:
        resolution = resolver.resolve(question)
        assert resolution.metric is not None, f"asked instead of binding: {resolution.question}"
        assert resolution.metric.key == expected

    def test_stemming_matches_singular_and_plural(self, resolver: MetricResolver) -> None:
        assert resolver.resolve("refund total").metric.key == "refunds"  # type: ignore[union-attr]

    def test_longest_phrase_wins_over_substring(self, resolver: MetricResolver) -> None:
        """'cost per customer' must beat marketing_spend's weaker 'cost' overlap."""
        resolution = resolver.resolve("customer acquisition cost")
        assert resolution.metric is not None
        assert resolution.metric.key == "cac"


class TestRestraint:
    """Declining to answer is the feature."""

    @pytest.mark.parametrize(
        "question",
        [
            "what is our conversion rate",   # undefined metric, shares only 'rate' with CTR
            "how is inventory looking",      # no such data at all
            "show me the numbers",           # every matched word is filler
            "how are we doing",              # names nothing
            "what about cost",               # spend or CAC, genuinely ambiguous
        ],
    )
    def test_asks_instead_of_guessing(self, resolver: MetricResolver, question: str) -> None:
        resolution = resolver.resolve(question)
        assert resolution.needs_clarification, (
            f"bound to {resolution.metric.key if resolution.metric else '?'} "
            f"on a question that does not name a metric"
        )
        assert resolution.question

    def test_clarifying_question_names_the_candidates(self, resolver: MetricResolver) -> None:
        """A question the user can answer in one word beats 'please rephrase'."""
        resolution = resolver.resolve("what about cost")
        assert resolution.question
        assert any(
            name.lower() in resolution.question.lower()
            for name in ("marketing spend", "customer acquisition cost", "cac")
        )

    def test_unknown_domain_term_is_surfaced(self, resolver: MetricResolver) -> None:
        assert "conversion" in " ".join(resolver.unknown_terms("what is our conversion rate"))

    def test_filler_is_not_treated_as_unknown(self, resolver: MetricResolver) -> None:
        """Time and quantity words say nothing about whether we understood."""
        assert resolver.unknown_terms("how many orders last quarter") == set()


class TestDimensionResolution:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("revenue by channel", "channel"),
            ("sales per region", "region"),
            ("revenue by market", "region"),
            ("spend by campaign", "campaign"),
            ("acquisitions by geo", "region"),
        ],
    )
    def test_grouping_phrase_decides(self, resolver: MetricResolver, question: str, expected: str) -> None:
        ranked = resolver.resolve_dimension(question)
        assert ranked, "no dimension matched"
        assert ranked[0][0].split(".")[-1] == expected

    def test_time_columns_are_not_grouping_dimensions(self, resolver: MetricResolver) -> None:
        """'ad spend by channel' must not rank spend_date first on the word 'spend'."""
        ranked = resolver.resolve_dimension("ad spend by channel")
        assert ranked[0][0].split(".")[-1] == "channel"
        assert all("date" not in key for key, _ in ranked)


class TestLoader:
    def test_yaml_on_keyword_is_repaired(self) -> None:
        """YAML 1.1 parses a bare `on:` key as boolean True. Analysts should not
        need to know that, so the loader fixes it rather than rejecting the pack."""
        model = load_dict(
            {
                "entities": [
                    {"key": "a", "dataset": "t_a", "dimensions": [{"key": "x"}]},
                    {"key": "b", "dataset": "t_b", "dimensions": [{"key": "y"}]},
                ],
                "joins": [{"left": "a", "right": "b", True: "a.id = b.id"}],
                "metrics": [{"key": "m", "entity": "a", "agg": "count", "sql": "{{entity}}.id"}],
            }
        )
        assert model.joins[0].on == "a.id = b.id"

    def test_every_problem_reported_at_once(self) -> None:
        with pytest.raises(SemanticLoadError) as exc:
            load_dict(
                {
                    "entities": [{"key": "a", "dataset": "t_a"}],
                    "metrics": [
                        {"key": "bad_entity", "entity": "nope", "agg": "sum", "sql": "x"},
                        {"key": "bad_ratio", "type": "ratio", "numerator": "ghost", "denominator": "ghost2"},
                    ],
                }
            )
        assert len(exc.value.problems) >= 2

    def test_metric_cycle_is_detected(self) -> None:
        with pytest.raises(SemanticLoadError, match="cycle"):
            load_dict(
                {
                    "entities": [{"key": "a", "dataset": "t_a"}],
                    "metrics": [
                        {"key": "x", "type": "derived", "expression": "y + 1"},
                        {"key": "y", "type": "derived", "expression": "x + 1"},
                    ],
                }
            )

    def test_type_is_accepted_as_alias_for_kind(self) -> None:
        model = load_dict(
            {
                "entities": [{"key": "a", "dataset": "t_a"}],
                "metrics": [
                    {"key": "n", "entity": "a", "agg": "count", "sql": "{{entity}}.id"},
                    {"key": "r", "type": "ratio", "numerator": "n", "denominator": "n"},
                ],
            }
        )
        assert model.metric("r").kind == "ratio"
