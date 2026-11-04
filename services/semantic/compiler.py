"""Deterministic MetricRef -> SQL compiler.

No language model touches this file. Given the same `MetricRef` and the same
semantic model it emits byte-identical SQL, which is what makes compiled
queries testable and cacheable.

The four guarantees it enforces by construction (BLUEPRINT 8.3):

1. Metric filters are never lost. Test-account and cancelled-order exclusions
   are injected into every query that touches the metric.
2. Ratios are recomputed from components at the requested grain, never
   averaged across groups. Simpson's paradox is off by default.
3. Fan-out joins cannot inflate additive metrics. The compiler pre-aggregates
   in a CTE, or refuses.
4. Time is handled correctly: fiscal calendars, week start, and comparison
   windows of equal length so partial periods align.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import sqlglot
from sqlglot import exp

from .models import (
    AggType,
    Comparison,
    Entity,
    Filter,
    Join,
    Metric,
    MetricRef,
    SemanticError,
    SemanticModel,
    TimeGrain,
    TimeIntelligence,
    TimeRange,
)

PERIOD_COLUMN = "period"
VALUE_COLUMN = "value"
PERIOD_TYPE_COLUMN = "period_type"

#: SQL is authored in this dialect and transpiled out by SQLGlot.
AUTHORING_DIALECT = "postgres"


class CompileError(SemanticError):
    """The MetricRef cannot be compiled into correct SQL."""


class FanOutError(CompileError):
    """Aggregating this metric across this dimension would double-count rows.

    Raised rather than silently emitting inflated numbers. Pre-aggregation
    handles the one-hop case; anything deeper is refused on purpose.
    """


class DimensionNotShared(CompileError):
    """A cross-entity ratio was asked to split by a dimension only one side has.

    e.g. CAC by `product_category`: spend has no product grain, so any number
    we produced would be an invention.
    """


@dataclass
class CompiledQuery:
    sql: str
    dialect: str
    metric: str
    grain: TimeGrain | None
    dimensions: list[str]
    comparison: Comparison
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def value_column(self) -> str:
        return VALUE_COLUMN


class Compiler:
    def __init__(self, model: SemanticModel, dialect: str = "duckdb") -> None:
        self.model = model
        self.dialect = dialect
        self.ti = model.time_intelligence

    # ---- public API ----------------------------------------------------

    def compile(self, ref: MetricRef) -> CompiledQuery:
        metric = self.model.metric(ref.metric)
        notes: list[str] = []

        if ref.comparison is not Comparison.NONE:
            sql = self._compile_with_comparison(metric, ref, notes)
        else:
            sql = self._compile_single(metric, ref, notes)

        parsed = sqlglot.parse_one(sql, read=AUTHORING_DIALECT)
        if not isinstance(parsed, exp.Expression):  # pragma: no cover - our own SQL
            raise CompileError(f"compiler produced unparseable SQL for '{metric.key}'")
        tree: exp.Expression = parsed
        out = tree.sql(dialect=self.dialect, pretty=True)

        return CompiledQuery(
            sql=out,
            dialect=self.dialect,
            metric=metric.key,
            grain=ref.grain,
            dimensions=list(ref.dimensions),
            comparison=ref.comparison,
            tables=_physical_tables(tree),
            columns=sorted({c.name for c in tree.find_all(exp.Column) if c.name}),
            notes=notes,
        )

    # ---- single-window compilation --------------------------------------

    def _compile_single(self, metric: Metric, ref: MetricRef, notes: list[str]) -> str:
        if self._needs_cte_join(metric, ref):
            return self._compile_cross_entity(metric, ref, notes)
        return self._compile_inline(metric, ref, notes)

    def _compile_inline(
        self,
        metric: Metric,
        ref: MetricRef,
        notes: list[str],
        *,
        time_range: TimeRange | None = None,
        extra_select: str | None = None,
        ordered: bool = True,
    ) -> str:
        """Everything resolves to one entity (plus safe many_to_one joins)."""
        base = self.model.base_entity(metric)
        alias = base.key
        time_range = time_range if time_range is not None else ref.time_range

        if ref.grain and ref.align_to_grain and time_range is not None:
            time_range, alignment_note = self._align_to_grain(time_range, ref.grain)
            if alignment_note:
                notes.append(alignment_note)

        select_parts: list[str] = []
        group_parts: list[str] = []

        if extra_select:
            select_parts.append(extra_select)

        if ref.grain:
            _, time_col = self.model.resolve_time_column(metric)
            period = self._grain_expression(f"{alias}.{time_col}", ref.grain)
            select_parts.append(f"{period} AS {PERIOD_COLUMN}")
            group_parts.append(period)

        joins, dim_exprs = self._resolve_dimensions(metric, base, ref.dimensions, notes)
        for dim_key, expr in dim_exprs:
            select_parts.append(f"{expr} AS {_quote_ident(dim_key)}")
            group_parts.append(expr)

        select_parts.append(f"{self._value_expression(metric, alias)} AS {VALUE_COLUMN}")

        sql = f"SELECT {', '.join(select_parts)}\nFROM {base.dataset} AS {alias}"
        for join_sql in joins:
            sql += f"\n{join_sql}"

        where = self._where_clauses(metric, alias, ref.filters, time_range)
        if where:
            sql += "\nWHERE " + " AND ".join(where)
        if group_parts:
            sql += "\nGROUP BY " + ", ".join(group_parts)
        if ordered:
            sql += self._order_and_limit(ref)
        return sql

    # ---- cross-entity ratios and derived metrics -------------------------

    def _needs_cte_join(self, metric: Metric, ref: MetricRef) -> bool:
        """True when components of the metric live on different base entities."""
        if metric.kind == "simple":
            return False
        components = self._component_metrics(metric)
        bases = {self.model.base_entity(self.model.metric(c)).key for c in components}
        return len(bases) > 1

    def _component_metrics(self, metric: Metric) -> list[str]:
        if metric.kind == "ratio":
            assert metric.numerator and metric.denominator
            return [metric.numerator, metric.denominator]
        if metric.kind == "derived":
            return self.model.derived_dependencies(metric)
        return [metric.key]

    def _compile_cross_entity(self, metric: Metric, ref: MetricRef, notes: list[str]) -> str:
        """Aggregate each side independently, then join on the shared grain.

        CAC is the motivating case: marketing spend and new customers live in
        different tables with different grains. Aggregating each side first and
        joining on (period, shared dimensions) is the only way to get a number
        that is not an artifact of a row-level join.
        """
        components = self._component_metrics(metric)
        if metric.kind != "ratio":
            raise CompileError(
                f"metric '{metric.key}' spans multiple entities and is not a ratio; "
                f"cross-entity derived metrics are not supported yet"
            )
        assert metric.numerator and metric.denominator

        keys: list[str] = []
        if ref.grain:
            keys.append(PERIOD_COLUMN)
        keys.extend(_quote_ident(d.split(".")[-1]) for d in ref.dimensions)

        self._assert_dimensions_shared(metric, components, ref.dimensions)

        ctes: list[str] = []
        for side, metric_key in (("numerator", metric.numerator), ("denominator", metric.denominator)):
            component = self.model.metric(metric_key)
            sub_ref = MetricRef(
                metric=metric_key,
                dimensions=ref.dimensions,
                filters=ref.filters,
                grain=ref.grain,
                time_range=ref.time_range,
            )
            ctes.append(
                f"{side} AS (\n{self._compile_inline(component, sub_ref, notes, ordered=False)}\n)"
            )

        notes.append(
            f"'{metric.key}' recomputed from {metric.numerator} / {metric.denominator} "
            f"at {ref.grain.value if ref.grain else 'total'} grain rather than averaged"
        )

        # Keys are referenced bare, not qualified. With FULL OUTER JOIN ... USING,
        # the unqualified name is the coalesced value, so a period present on only
        # one side still carries its key instead of appearing as NULL. Writing
        # `numerator.period` here would silently null out every row where there was
        # spend but no customers, or vice versa — which is exactly the interesting case.
        select_keys = ", ".join(keys)
        join_clause = (
            f"FULL OUTER JOIN denominator USING ({', '.join(keys)})" if keys else "CROSS JOIN denominator"
        )
        value = f"numerator.{VALUE_COLUMN} / NULLIF(denominator.{VALUE_COLUMN}, 0) AS {VALUE_COLUMN}"
        projection = f"{select_keys}, {value}" if select_keys else value

        sql = "WITH " + ",\n".join(ctes) + f"\nSELECT {projection}\nFROM numerator\n{join_clause}"
        if PERIOD_COLUMN in keys:
            sql += f"\nORDER BY {PERIOD_COLUMN}"
        if ref.limit:
            sql += f"\nLIMIT {int(ref.limit)}"
        return sql

    def _assert_dimensions_shared(
        self, metric: Metric, components: list[str], dimensions: list[str]
    ) -> None:
        for dim_key in dimensions:
            bare = dim_key.split(".")[-1]
            missing = []
            for component_key in components:
                base = self.model.base_entity(self.model.metric(component_key))
                if bare not in base.dimension_map and not self._reachable_dimension(base, bare):
                    missing.append(base.key)
            if missing:
                raise DimensionNotShared(
                    f"cannot split '{metric.key}' by '{dim_key}': "
                    f"{', '.join(missing)} has no such dimension, so the split would be fabricated"
                )

    def _reachable_dimension(self, base: Entity, dim_key: str) -> bool:
        for join in self.model.joins:
            other_key = join.right if join.left == base.key else join.left if join.right == base.key else None
            if other_key and dim_key in self.model.entity(other_key).dimension_map:
                return True
        return False

    # ---- comparison windows ---------------------------------------------

    def _compile_with_comparison(self, metric: Metric, ref: MetricRef, notes: list[str]) -> str:
        """Union the current window with an equal-length shifted window.

        Shifting by exactly the window length (or one year) keeps both sides the
        same number of days, so a month-to-date figure is compared against a
        month-to-date figure. Comparing a partial period against a full one is
        treated as a bug, not a caveat (BLUEPRINT 8.4).
        """
        if ref.time_range is None:
            raise CompileError(
                f"comparison '{ref.comparison.value}' requires an explicit time_range "
                f"so the comparison window can be aligned to the same length"
            )

        prior = self._shift_range(ref.time_range, ref.comparison)
        notes.append(
            f"comparing {ref.time_range.start}..{ref.time_range.end} against "
            f"{prior.start}..{prior.end} ({(ref.time_range.end - ref.time_range.start).days + 1} days each)"
        )

        if self._needs_cte_join(metric, ref):
            raise CompileError(
                f"metric '{metric.key}' spans multiple entities; period comparison for "
                f"cross-entity ratios is not supported yet"
            )

        current = self._compile_inline(
            metric, ref, notes, time_range=ref.time_range, extra_select="'current' AS " + PERIOD_TYPE_COLUMN
        )
        baseline = self._compile_inline(
            metric, ref, notes, time_range=prior, extra_select="'comparison' AS " + PERIOD_TYPE_COLUMN
        )
        return f"{current}\nUNION ALL\n{baseline}"

    def _align_to_grain(self, window: TimeRange, grain: TimeGrain) -> tuple[TimeRange, str | None]:
        """Trim a window to whole periods at the requested grain.

        A trend whose first bucket covers two days of a week, or whose last bucket
        is the current half-finished month, renders a cliff at each end that looks
        exactly like a collapse. Users read the cliff as a business event; it is an
        artifact of where the window happened to start.

        Trimming rather than extending is deliberate: extending would invent data
        outside the range the caller asked for.
        """
        start = _period_start(window.start, grain, self.ti)
        if start < window.start:
            start = _next_period_start(window.start, grain, self.ti)

        last_start = _period_start(window.end, grain, self.ti)
        last_end = _next_period_start(last_start, grain, self.ti) - timedelta(days=1)
        end = window.end if last_end <= window.end else last_start - timedelta(days=1)

        if start > end:
            # The window is narrower than one period. Honour it as given rather
            # than returning an empty result.
            return window, (
                f"window {window.start}..{window.end} is shorter than one {grain.value}; "
                f"showing it unaligned, so the period is partial"
            )

        if (start, end) == (window.start, window.end):
            return window, None

        return TimeRange(start=start, end=end, label=window.label), (
            f"window trimmed to whole {grain.value}s "
            f"({window.start}..{window.end} -> {start}..{end}) so partial periods "
            f"are not plotted beside complete ones"
        )

    def _shift_range(self, window: TimeRange, comparison: Comparison) -> TimeRange:
        if comparison is Comparison.PRIOR_YEAR:
            return TimeRange(
                start=_shift_years(window.start, -1), end=_shift_years(window.end, -1), label="prior_year"
            )
        span = (window.end - window.start).days + 1
        return TimeRange(
            start=window.start - timedelta(days=span),
            end=window.end - timedelta(days=span),
            label="prior_period",
        )

    # ---- expressions -----------------------------------------------------

    def _value_expression(self, metric: Metric, alias: str) -> str:
        """The aggregate that produces the metric's value, within one entity."""
        if metric.kind == "simple":
            assert metric.agg is not None
            inner = (metric.sql or "*").replace("{{entity}}", alias)
            return _aggregate(metric.agg, inner)

        if metric.kind == "ratio":
            assert metric.numerator and metric.denominator
            num = self._value_expression(self.model.metric(metric.numerator), alias)
            den = self._value_expression(self.model.metric(metric.denominator), alias)
            # Recomputed from components, never AVG(ratio).
            return f"({num}) / NULLIF({den}, 0)"

        assert metric.expression is not None
        expression = metric.expression
        for dep in sorted(self.model.derived_dependencies(metric), key=len, reverse=True):
            dep_expr = self._value_expression(self.model.metric(dep), alias)
            expression = _replace_identifier(expression, dep, f"COALESCE({dep_expr}, 0)")
        return f"({expression})"

    def _grain_expression(self, column: str, grain: TimeGrain) -> str:
        if grain is TimeGrain.WEEK and self.ti.week_starts_on == "sunday":
            # Postgres and DuckDB both truncate weeks to Monday. Shift in, shift out.
            return (
                f"(DATE_TRUNC('week', {column} + INTERVAL '1 day') - INTERVAL '1 day')"
            )

        if grain in (TimeGrain.QUARTER, TimeGrain.YEAR) and self.ti.fiscal_start_month != 1:
            offset = self.ti.fiscal_start_month - 1
            return (
                f"(DATE_TRUNC('{grain.value}', {column} - INTERVAL '{offset} months')"
                f" + INTERVAL '{offset} months')"
            )

        return f"DATE_TRUNC('{grain.value}', {column})"

    def _resolve_dimensions(
        self, metric: Metric, base: Entity, dimensions: list[str], notes: list[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Resolve dimension keys to SQL, adding joins and refusing fan-out."""
        joins: list[str] = []
        joined: set[str] = set()
        exprs: list[tuple[str, str]] = []

        for dim_key in dimensions:
            entity, dim = self.model.find_dimension(dim_key, prefer=base.key)
            if entity.key == base.key:
                exprs.append((dim.key, dim.expression(base.key)))
                continue

            join = self.model.join_between(base.key, entity.key)
            if join is None:
                raise CompileError(
                    f"no join defined between '{base.key}' and '{entity.key}', "
                    f"so '{metric.key}' cannot be split by '{dim_key}'"
                )

            if join.type.fans_out:
                assert metric.agg is not None or metric.kind != "simple"
                additive = metric.kind == "simple" and metric.agg is not None and metric.agg.is_additive
                raise FanOutError(
                    f"splitting '{metric.key}' by '{dim_key}' follows a one-to-many join "
                    f"from '{base.key}' to '{entity.key}', which would "
                    f"{'double-count' if additive else 'distort'} the metric. "
                    f"Define the metric on '{entity.key}' instead, or pre-aggregate it there."
                )

            if entity.key not in joined:
                joins.append(f"LEFT JOIN {entity.dataset} AS {entity.key} ON {self._join_on(join)}")
                joined.add(entity.key)
                notes.append(f"joined '{entity.key}' ({join.type.value}) to resolve '{dim_key}'")
            exprs.append((dim.key, dim.expression(entity.key)))

        return joins, exprs

    def _join_on(self, join: Join) -> str:
        return join.on

    def _where_clauses(
        self,
        metric: Metric,
        alias: str,
        filters: list[Filter],
        time_range: TimeRange | None,
    ) -> list[str]:
        clauses: list[str] = []

        # Guarantee 1: metric-level filters, always, including for every metric a
        # ratio or derived metric is built from.
        for metric_key in self._all_filter_sources(metric):
            for raw in self.model.metric(metric_key).filters:
                clauses.append(f"({raw.replace('{{entity}}', alias)})")

        if time_range is not None:
            _, time_col = self.model.resolve_time_column(metric)
            clauses.append(
                f"{alias}.{time_col} >= DATE '{time_range.start.isoformat()}' "
                f"AND {alias}.{time_col} < DATE '{(time_range.end + timedelta(days=1)).isoformat()}'"
            )

        for f in filters:
            clauses.append(self._filter_clause(f, alias))

        return _dedupe(clauses)

    def _all_filter_sources(self, metric: Metric) -> list[str]:
        """A metric's own filters plus those of every metric it is composed from."""
        seen: list[str] = [metric.key]
        if metric.kind in ("ratio", "derived"):
            for component in self._component_metrics(metric):
                if component not in seen:
                    seen.extend(self._all_filter_sources(self.model.metric(component)))
        return _dedupe(seen)

    def _filter_clause(self, f: Filter, base_alias: str) -> str:
        entity, dim = self.model.find_dimension(f.dimension, prefer=base_alias)
        column = dim.expression(entity.key)
        literals = [_literal(v) for v in f.values]

        if f.operator in ("in", "not_in"):
            negate = "NOT " if f.operator == "not_in" else ""
            return f"{column} {negate}IN ({', '.join(literals)})"
        if f.operator == "like":
            return f"{column} LIKE {literals[0]}"
        if not literals:
            raise CompileError(f"filter on '{f.dimension}' has no values")
        return f"{column} {f.operator} {literals[0]}"

    def _order_and_limit(self, ref: MetricRef) -> str:
        out = ""
        if ref.order_by_value:
            out += f"\nORDER BY {VALUE_COLUMN} DESC NULLS LAST"
        elif ref.grain:
            out += f"\nORDER BY {PERIOD_COLUMN}"
        if ref.limit:
            out += f"\nLIMIT {int(ref.limit)}"
        return out


# ---- helpers -----------------------------------------------------------


def _period_start(value: date, grain: TimeGrain, ti: TimeIntelligence) -> date:
    """First day of the period containing `value`, honouring week start and fiscal year."""
    if grain in (TimeGrain.DAY, TimeGrain.HOUR):
        return value
    if grain is TimeGrain.WEEK:
        offset = value.weekday() if ti.week_starts_on == "monday" else (value.weekday() + 1) % 7
        return value - timedelta(days=offset)
    if grain is TimeGrain.MONTH:
        return value.replace(day=1)
    if grain is TimeGrain.QUARTER:
        fiscal_offset = (value.month - ti.fiscal_start_month) % 3
        month = value.month - fiscal_offset
        year = value.year
        if month < 1:
            month += 12
            year -= 1
        return date(year, month, 1)
    # YEAR
    start = date(value.year, ti.fiscal_start_month, 1)
    return start if start <= value else date(value.year - 1, ti.fiscal_start_month, 1)


def _next_period_start(value: date, grain: TimeGrain, ti: TimeIntelligence) -> date:
    current = _period_start(value, grain, ti)
    if grain in (TimeGrain.DAY, TimeGrain.HOUR):
        return current + timedelta(days=1)
    if grain is TimeGrain.WEEK:
        return current + timedelta(days=7)
    months = {TimeGrain.MONTH: 1, TimeGrain.QUARTER: 3, TimeGrain.YEAR: 12}[grain]
    month = current.month + months
    year = current.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, 1)


def _physical_tables(tree: exp.Expression) -> list[str]:
    """Qualified table names without aliases, and without CTE names.

    `Table.sql()` renders "fct_orders AS orders", which is a rendering, not an
    identity. Lineage that carries the alias cannot be matched against a
    connection's table list or grouped across queries that alias differently.
    """
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        parts = [p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p]
        qualified = ".".join(parts)
        if qualified and qualified.lower() not in cte_names:
            names.add(qualified)
    return sorted(names)


def _aggregate(agg: AggType, inner: str) -> str:
    match agg:
        case AggType.SUM:
            return f"SUM({inner})"
        case AggType.COUNT:
            return f"COUNT({inner})"
        case AggType.COUNT_DISTINCT:
            return f"COUNT(DISTINCT {inner})"
        case AggType.AVG:
            return f"AVG({inner})"
        case AggType.MIN:
            return f"MIN({inner})"
        case AggType.MAX:
            return f"MAX({inner})"
        case AggType.MEDIAN:
            return f"MEDIAN({inner})"
    raise CompileError(f"unsupported aggregate {agg}")


def _literal(value: str | float | bool) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _replace_identifier(expression: str, identifier: str, replacement: str) -> str:
    import re

    return re.sub(rf"\b{re.escape(identifier)}\b", replacement, expression)


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:  # 29 Feb in a non-leap year
        return value.replace(year=value.year + years, day=28)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
