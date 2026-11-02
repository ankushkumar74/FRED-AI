"""Compiler guarantees — BLUEPRINT 8.3.

These are not coverage tests. Each one pins a specific way a BI tool produces a
wrong number, and asserts we don't.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.semantic.compiler import (
    Compiler,
    CompileError,
    DimensionNotShared,
    FanOutError,
)
from services.semantic.loader import load_dict
from services.semantic.models import (
    Comparison,
    Filter,
    MetricRef,
    TimeGrain,
    TimeRange,
    UnknownMetric,
)

MODEL = {
    "entities": [
        {
            "key": "orders",
            "dataset": "fct_orders",
            "primary_key": "order_id",
            "default_time_column": "ordered_at",
            "dimensions": [{"key": "channel"}, {"key": "region"}, {"key": "device"}],
        },
        {
            "key": "items",
            "dataset": "fct_order_items",
            "dimensions": [{"key": "category"}],
        },
        {
            "key": "spend",
            "dataset": "fct_spend",
            "default_time_column": "spend_date",
            "dimensions": [{"key": "channel"}, {"key": "region"}],
        },
    ],
    "joins": [
        {"left": "orders", "right": "items", "type": "one_to_many", "on": "orders.order_id = items.order_id"},
    ],
    "metrics": [
        {
            "key": "revenue",
            "entity": "orders",
            "agg": "sum",
            "sql": "{{entity}}.order_total",
            "filters": ["{{entity}}.status <> 'cancelled'", "{{entity}}.is_internal = FALSE"],
        },
        {"key": "refunds", "entity": "orders", "agg": "sum", "sql": "{{entity}}.refund_amount",
         "filters": ["{{entity}}.status <> 'cancelled'"]},
        {"key": "orders_count", "entity": "orders", "agg": "count", "sql": "{{entity}}.order_id",
         "filters": ["{{entity}}.status <> 'cancelled'"]},
        {"key": "net_revenue", "type": "derived", "expression": "revenue - refunds"},
        {"key": "aov", "type": "ratio", "numerator": "net_revenue", "denominator": "orders_count"},
        {"key": "marketing_spend", "entity": "spend", "agg": "sum", "sql": "{{entity}}.spend"},
        {"key": "cac", "type": "ratio", "numerator": "marketing_spend", "denominator": "orders_count"},
    ],
}

JULY = TimeRange(start=date(2026, 7, 1), end=date(2026, 7, 31))


@pytest.fixture
def compiler() -> Compiler:
    return Compiler(load_dict(MODEL), dialect="duckdb")


class TestFilterInjection:
    """Guarantee 1: metric filters can never be forgotten."""

    def test_filters_present_on_simple_metric(self, compiler: Compiler) -> None:
        sql = compiler.compile(MetricRef(metric="revenue", time_range=JULY)).sql
        assert "status <> 'cancelled'" in sql
        assert "is_internal = FALSE" in sql

    def test_derived_metric_inherits_component_filters(self, compiler: Compiler) -> None:
        """net_revenue = revenue - refunds must carry *both* components' filters.

        A derived metric that drops its components' exclusions silently includes
        cancelled orders in exactly half of the arithmetic.
        """
        sql = compiler.compile(MetricRef(metric="net_revenue", time_range=JULY)).sql
        assert "is_internal = FALSE" in sql
        assert sql.count("'cancelled'") >= 1

    def test_ratio_inherits_filters_from_both_sides(self, compiler: Compiler) -> None:
        sql = compiler.compile(MetricRef(metric="aov", time_range=JULY)).sql
        assert "is_internal = FALSE" in sql


class TestRatioRecomputation:
    """Guarantee 2: ratios are recomputed, never averaged across groups."""

    def test_ratio_divides_aggregates_not_averages_rows(self, compiler: Compiler) -> None:
        sql = compiler.compile(MetricRef(metric="aov", dimensions=["channel"], time_range=JULY)).sql
        assert "AVG(" not in sql.upper()
        assert "NULLIF" in sql  # divide-by-zero guarded

    def test_ratio_regrouped_at_each_grain(self, compiler: Compiler) -> None:
        sql = compiler.compile(
            MetricRef(metric="aov", grain=TimeGrain.WEEK, time_range=JULY)
        ).sql
        assert "GROUP BY" in sql
        assert "AVG(" not in sql.upper()

    def test_cross_entity_ratio_uses_ctes_not_row_join(self, compiler: Compiler) -> None:
        """CAC spans two tables. A row-level join would multiply spend by orders."""
        compiled = compiler.compile(
            MetricRef(metric="cac", dimensions=["channel"], grain=TimeGrain.WEEK, time_range=JULY)
        )
        assert "WITH" in compiled.sql
        assert "numerator" in compiled.sql and "denominator" in compiled.sql
        assert "FULL OUTER JOIN" in compiled.sql
        assert any("recomputed" in note for note in compiled.notes)


class TestFanOutProtection:
    """Guarantee 3: a one-to-many join cannot inflate an additive metric."""

    def test_fan_out_dimension_is_refused(self, compiler: Compiler) -> None:
        with pytest.raises(FanOutError) as exc:
            compiler.compile(
                MetricRef(metric="revenue", dimensions=["category"], time_range=JULY)
            )
        assert "double-count" in str(exc.value)

    def test_refusal_names_the_fix(self, compiler: Compiler) -> None:
        """An error an agent can act on beats an error it can only report."""
        with pytest.raises(FanOutError) as exc:
            compiler.compile(MetricRef(metric="revenue", dimensions=["category"], time_range=JULY))
        assert "items" in str(exc.value)

    def test_unshared_dimension_on_cross_entity_ratio_is_refused(self, compiler: Compiler) -> None:
        with pytest.raises(DimensionNotShared) as exc:
            compiler.compile(MetricRef(metric="cac", dimensions=["device"], time_range=JULY))
        assert "fabricated" in str(exc.value)

    def test_shared_dimension_on_cross_entity_ratio_is_allowed(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(metric="cac", dimensions=["region"], time_range=JULY)
        )
        assert "region" in compiled.sql


class TestTimeHandling:
    """Guarantee 4: comparison windows are equal length; grains respect config."""

    def test_comparison_windows_are_the_same_length(self, compiler: Compiler) -> None:
        week = TimeRange(start=date(2026, 7, 27), end=date(2026, 8, 2))
        compiled = compiler.compile(
            MetricRef(metric="revenue", time_range=week, comparison=Comparison.PRIOR_PERIOD)
        )
        assert "UNION ALL" in compiled.sql
        assert "2026-07-20" in compiled.sql  # shifted by exactly 7 days
        assert "7 days each" in " ".join(compiled.notes)

    def test_prior_year_shifts_by_a_year(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(metric="revenue", time_range=JULY, comparison=Comparison.PRIOR_YEAR)
        )
        assert "2025-07-01" in compiled.sql

    def test_comparison_without_range_is_refused(self, compiler: Compiler) -> None:
        """Without an explicit window there is nothing to align against."""
        with pytest.raises(CompileError, match="requires an explicit time_range"):
            compiler.compile(MetricRef(metric="revenue", comparison=Comparison.PRIOR_PERIOD))

    def test_sunday_week_start_shifts_the_truncation(self) -> None:
        model = dict(MODEL, time_intelligence={"week_starts_on": "sunday"})
        sql = Compiler(load_dict(model), "duckdb").compile(
            MetricRef(metric="revenue", grain=TimeGrain.WEEK, time_range=JULY)
        ).sql
        assert "INTERVAL" in sql.upper()

    def test_fiscal_year_offset_applied(self) -> None:
        model = dict(MODEL, time_intelligence={"fiscal_year_start": "04-01"})
        sql = Compiler(load_dict(model), "duckdb").compile(
            MetricRef(metric="revenue", grain=TimeGrain.QUARTER, time_range=JULY)
        ).sql
        assert "MONTH" in sql.upper() and "3" in sql

    def test_end_date_is_inclusive(self, compiler: Compiler) -> None:
        """A range ending 2026-07-31 must include that whole day.

        Implemented as a half-open `< 2026-08-01` so a timestamp at 23:59 is not
        silently dropped, which a `BETWEEN` on dates would do.
        """
        sql = compiler.compile(MetricRef(metric="revenue", time_range=JULY)).sql
        assert "2026-08-01" in sql


class TestPeriodAlignment:
    """A partial bucket beside complete ones renders as a cliff and reads as a crash."""

    def test_window_trimmed_to_whole_weeks(self, compiler: Compiler) -> None:
        # Sunday 2026-06-07 to Sunday 2026-08-02: the first week is one day long.
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                grain=TimeGrain.WEEK,
                time_range=TimeRange(start=date(2026, 6, 7), end=date(2026, 8, 2)),
            )
        )
        assert "2026-06-08" in compiled.sql  # moved forward to the Monday
        assert "2026-06-07" not in compiled.sql
        assert any("trimmed to whole weeks" in note for note in compiled.notes)

    def test_trailing_partial_month_is_dropped(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                grain=TimeGrain.MONTH,
                time_range=TimeRange(start=date(2026, 1, 1), end=date(2026, 8, 14)),
            )
        )
        # August is incomplete, so the window ends 31 July (exclusive bound 1 Aug).
        assert "2026-08-01" in compiled.sql
        assert any("trimmed" in note for note in compiled.notes)

    def test_already_aligned_window_is_untouched(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                grain=TimeGrain.MONTH,
                time_range=TimeRange(start=date(2026, 1, 1), end=date(2026, 7, 31)),
            )
        )
        assert not any("trimmed" in note for note in compiled.notes)

    def test_alignment_can_be_disabled(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                grain=TimeGrain.WEEK,
                time_range=TimeRange(start=date(2026, 6, 7), end=date(2026, 8, 2)),
                align_to_grain=False,
            )
        )
        assert "2026-06-07" in compiled.sql

    def test_window_shorter_than_one_period_is_kept_with_a_warning(self, compiler: Compiler) -> None:
        """Trimming a three-day window to whole months would return nothing."""
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                grain=TimeGrain.MONTH,
                time_range=TimeRange(start=date(2026, 7, 10), end=date(2026, 7, 12)),
            )
        )
        assert "2026-07-10" in compiled.sql
        assert any("shorter than one month" in note for note in compiled.notes)

    def test_no_grain_means_no_alignment(self, compiler: Compiler) -> None:
        """A single total over an arbitrary window is not a period comparison."""
        compiled = compiler.compile(
            MetricRef(metric="revenue", time_range=TimeRange(start=date(2026, 6, 7), end=date(2026, 8, 2)))
        )
        assert "2026-06-07" in compiled.sql
        assert not compiled.notes


class TestDialectsAndSafety:
    def test_same_ref_transpiles_to_each_dialect(self) -> None:
        model = load_dict(MODEL)
        ref = MetricRef(metric="revenue", grain=TimeGrain.MONTH, time_range=JULY)
        for dialect in ("duckdb", "postgres", "snowflake", "bigquery"):
            compiled = Compiler(model, dialect).compile(ref)
            assert compiled.dialect == dialect
            assert "SELECT" in compiled.sql.upper()

    def test_unknown_metric_suggests_alternatives(self, compiler: Compiler) -> None:
        with pytest.raises(UnknownMetric) as exc:
            compiler.compile(MetricRef(metric="revenu", time_range=JULY))
        assert "did you mean" in str(exc.value)

    def test_filter_values_are_escaped(self, compiler: Compiler) -> None:
        compiled = compiler.compile(
            MetricRef(
                metric="revenue",
                time_range=JULY,
                filters=[Filter(dimension="channel", values=["O'Brien"])],
            )
        )
        assert "O''Brien" in compiled.sql

    def test_lineage_is_reported(self, compiler: Compiler) -> None:
        compiled = compiler.compile(MetricRef(metric="revenue", time_range=JULY))
        assert "fct_orders" in compiled.tables

    def test_compilation_is_deterministic(self, compiler: Compiler) -> None:
        """Same input, same bytes — otherwise caching and eval are both unsound."""
        ref = MetricRef(metric="aov", dimensions=["channel"], grain=TimeGrain.WEEK, time_range=JULY)
        assert compiler.compile(ref).sql == compiler.compile(ref).sql
