"""Semantic layer domain models.

These types are the closed vocabulary the LLM is allowed to produce. A planner
emits a `MetricRef`, never SQL — a `MetricRef` referencing an unknown metric
fails validation loudly, whereas hallucinated SQL fails silently and returns a
wrong number. That asymmetry is the whole reason this module exists.

See BLUEPRINT Part 8.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class TimeGrain(str, Enum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"

    @property
    def is_sub_daily(self) -> bool:
        return self is TimeGrain.HOUR


class AggType(str, Enum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    MEDIAN = "median"

    @property
    def is_additive(self) -> bool:
        """Additive aggregates can be summed across disjoint slices.

        AVG and MEDIAN cannot, which is why a fan-out join or a naive
        cross-segment rollup corrupts them in ways SUM survives.
        """
        return self in (AggType.SUM, AggType.COUNT)


class JoinType(str, Enum):
    MANY_TO_ONE = "many_to_one"
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"

    @property
    def fans_out(self) -> bool:
        """True when following this join multiplies rows on the left side.

        Aggregating an additive metric across a fan-out join double-counts it.
        This is the single most common wrong-number bug in hand-written BI SQL
        and the compiler refuses to produce it (see compiler.FanOutError).
        """
        return self is JoinType.ONE_TO_MANY


class Format(str, Enum):
    NUMBER = "number"
    CURRENCY = "currency"
    PERCENT = "percent"
    DURATION = "duration"


class Dimension(BaseModel):
    key: str
    label: str | None = None
    sql: str | None = Field(
        default=None,
        description="SQL expression; '{{entity}}' is substituted with the table alias. "
        "Defaults to the column named `key`.",
    )
    data_type: Literal["string", "number", "boolean", "date", "timestamp"] = "string"
    is_time: bool = False
    synonyms: list[str] = Field(default_factory=list)
    description: str | None = None

    @property
    def display(self) -> str:
        return self.label or self.key.replace("_", " ").title()

    def expression(self, alias: str) -> str:
        return (self.sql or f"{{{{entity}}}}.{self.key}").replace("{{entity}}", alias)


class Entity(BaseModel):
    key: str
    dataset: str = Field(description="Physical table or view, e.g. analytics.fct_orders")
    primary_key: str | None = None
    default_time_column: str | None = None
    dimensions: list[Dimension] = Field(default_factory=list)
    description: str | None = None

    @property
    def dimension_map(self) -> dict[str, Dimension]:
        return {d.key: d for d in self.dimensions}


class Join(BaseModel):
    left: str
    right: str
    type: JoinType = JoinType.MANY_TO_ONE
    on: str = Field(description="Join predicate using entity keys, e.g. 'orders.customer_id = customers.customer_id'")


class Metric(BaseModel):
    key: str
    label: str | None = None
    description: str | None = None

    kind: Literal["simple", "ratio", "derived"] = "simple"

    # kind == "simple"
    entity: str | None = None
    agg: AggType | None = None
    sql: str | None = Field(
        default=None, description="Expression to aggregate; '{{entity}}' is the table alias."
    )

    # kind == "ratio" — ALWAYS recomputed from components at the requested grain,
    # never averaged across groups. See BLUEPRINT 8.3 point 2.
    numerator: str | None = None
    denominator: str | None = None

    # kind == "derived"
    expression: str | None = Field(
        default=None, description="Arithmetic over other metric keys, e.g. 'gross_revenue - refunds'"
    )

    filters: list[str] = Field(
        default_factory=list,
        description="Always injected. Test-account and cancelled-order exclusions live here, "
        "which is why they can never be forgotten by a query author.",
    )
    time_column: str | None = None
    format: Format = Format.NUMBER
    unit: str | None = None
    higher_is_better: bool = True
    synonyms: list[str] = Field(default_factory=list)
    certified: bool = False
    owner: str | None = None
    version: int = 1

    @model_validator(mode="after")
    def _check_kind_fields(self) -> Metric:
        if self.kind == "simple":
            if not self.entity or not self.agg:
                raise ValueError(f"metric '{self.key}': simple metrics need `entity` and `agg`")
            if self.agg not in (AggType.COUNT,) and not self.sql:
                raise ValueError(f"metric '{self.key}': `sql` is required for agg={self.agg.value}")
        elif self.kind == "ratio":
            if not (self.numerator and self.denominator):
                raise ValueError(f"metric '{self.key}': ratio metrics need `numerator` and `denominator`")
        elif self.kind == "derived":
            if not self.expression:
                raise ValueError(f"metric '{self.key}': derived metrics need `expression`")
        return self

    @property
    def display(self) -> str:
        return self.label or self.key.replace("_", " ").title()

    @property
    def search_text(self) -> str:
        """Everything the resolver matches against, lexically and semantically."""
        parts = [self.key.replace("_", " "), self.display, *self.synonyms]
        if self.description:
            parts.append(self.description)
        return " ".join(parts)


class TimeIntelligence(BaseModel):
    fiscal_year_start: str = Field(default="01-01", description="MM-DD")
    week_starts_on: Literal["monday", "sunday"] = "monday"
    default_comparisons: list[str] = Field(default_factory=lambda: ["prior_period", "prior_year"])
    timezone: str = "UTC"

    @property
    def fiscal_start_month(self) -> int:
        return int(self.fiscal_year_start.split("-")[0])


class Comparison(str, Enum):
    NONE = "none"
    PRIOR_PERIOD = "prior_period"
    PRIOR_YEAR = "prior_year"


class Filter(BaseModel):
    """A user- or agent-supplied filter, resolved against profiled column values.

    `values` are matched against the real distinct values of the column before
    this object is constructed — the resolver never invents a filter value it
    has not seen in the data (BLUEPRINT 8.5).
    """

    dimension: str
    operator: Literal["=", "!=", "in", "not_in", ">", ">=", "<", "<=", "like"] = "="
    values: list[str | float | bool]


class TimeRange(BaseModel):
    start: date
    end: date
    label: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> TimeRange:
        if self.start > self.end:
            raise ValueError("time range start is after end")
        return self


class MetricRef(BaseModel):
    """The planner's output. Closed vocabulary, fully validated before compilation."""

    metric: str
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    grain: TimeGrain | None = None
    time_range: TimeRange | None = None
    comparison: Comparison = Comparison.NONE
    limit: int | None = None
    order_by_value: bool = False
    align_to_grain: bool = Field(
        default=True,
        description="Trim the window to whole periods. A half-finished week plotted "
        "beside complete ones looks like a collapse and is the most common source of "
        "false panic in BI; set False only when the partial period is the point.",
    )


class SemanticModel(BaseModel):
    """The loaded, validated semantic layer for one project."""

    version: int = 1
    entities: list[Entity] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    time_intelligence: TimeIntelligence = Field(default_factory=TimeIntelligence)

    # ---- lookups -------------------------------------------------------

    @property
    def entity_map(self) -> dict[str, Entity]:
        return {e.key: e for e in self.entities}

    @property
    def metric_map(self) -> dict[str, Metric]:
        return {m.key: m for m in self.metrics}

    def entity(self, key: str) -> Entity:
        try:
            return self.entity_map[key]
        except KeyError:
            raise UnknownEntity(key, sorted(self.entity_map)) from None

    def metric(self, key: str) -> Metric:
        try:
            return self.metric_map[key]
        except KeyError:
            raise UnknownMetric(key, sorted(self.metric_map)) from None

    def find_dimension(self, key: str, prefer: str | None = None) -> tuple[Entity, Dimension]:
        """Resolve a dimension key, accepting either 'channel' or 'orders.channel'.

        `prefer` is the entity being queried. A dimension like `channel` legitimately
        exists on several entities (orders and marketing_spend both have one), and
        resolving it against the entity we are already aggregating is both correct
        and what the user meant — so that is not an ambiguity worth bothering them
        about. Genuine ambiguity, where the key is on neither the preferred entity
        nor exactly one other, still raises.
        """
        if "." in key:
            entity_key, dim_key = key.split(".", 1)
            entity = self.entity(entity_key)
            dim = entity.dimension_map.get(dim_key)
            if dim is None:
                raise UnknownDimension(key, self._all_dimension_keys())
            return entity, dim

        if prefer:
            preferred = self.entity_map.get(prefer)
            if preferred and key in preferred.dimension_map:
                return preferred, preferred.dimension_map[key]

        matches = [(e, e.dimension_map[key]) for e in self.entities if key in e.dimension_map]
        if not matches:
            raise UnknownDimension(key, self._all_dimension_keys())
        if len(matches) > 1:
            owners = ", ".join(f"{e.key}.{key}" for e, _ in matches)
            raise AmbiguousDimension(key, owners)
        return matches[0]

    def _all_dimension_keys(self) -> list[str]:
        return sorted({f"{e.key}.{d.key}" for e in self.entities for d in e.dimensions})

    def join_between(self, left: str, right: str) -> Join | None:
        """Find a join in either direction, inverting the cardinality if reversed."""
        for j in self.joins:
            if j.left == left and j.right == right:
                return j
            if j.left == right and j.right == left:
                inverted = {
                    JoinType.MANY_TO_ONE: JoinType.ONE_TO_MANY,
                    JoinType.ONE_TO_MANY: JoinType.MANY_TO_ONE,
                    JoinType.ONE_TO_ONE: JoinType.ONE_TO_ONE,
                }[j.type]
                return Join(left=left, right=right, type=inverted, on=j.on)
        return None

    def resolve_time_column(self, metric: Metric) -> tuple[Entity, str]:
        """The time column a metric is measured on, with a clear error if absent."""
        base = self.base_entity(metric)
        column = metric.time_column or base.default_time_column
        if not column:
            raise MissingTimeColumn(metric.key, base.key)
        return base, column

    def base_entity(self, metric: Metric) -> Entity:
        """The entity a metric ultimately aggregates over."""
        if metric.kind == "simple":
            assert metric.entity is not None
            return self.entity(metric.entity)
        if metric.kind == "ratio":
            assert metric.numerator is not None
            return self.base_entity(self.metric(metric.numerator))
        assert metric.expression is not None
        for dep in self.derived_dependencies(metric):
            return self.base_entity(self.metric(dep))
        raise ValueError(f"metric '{metric.key}': derived expression references no known metric")

    def derived_dependencies(self, metric: Metric) -> list[str]:
        """Metric keys referenced by a derived metric's expression."""
        import re

        assert metric.expression is not None
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", metric.expression)
        known = self.metric_map
        return [t for t in tokens if t in known]

    def validate_graph(self) -> list[str]:
        """Structural checks the YAML schema cannot express. Returns problems."""
        problems: list[str] = []
        metrics = self.metric_map
        entities = self.entity_map

        for m in self.metrics:
            if m.kind == "simple" and m.entity not in entities:
                problems.append(f"metric '{m.key}' references unknown entity '{m.entity}'")
            if m.kind == "ratio":
                for role, ref in (("numerator", m.numerator), ("denominator", m.denominator)):
                    if ref not in metrics:
                        problems.append(f"metric '{m.key}' {role} references unknown metric '{ref}'")
            if m.kind == "derived" and not self.derived_dependencies(m):
                problems.append(
                    f"metric '{m.key}' expression '{m.expression}' references no known metric"
                )

        for j in self.joins:
            for side in (j.left, j.right):
                if side not in entities:
                    problems.append(f"join references unknown entity '{side}'")

        problems.extend(self._detect_cycles())
        return problems

    def _detect_cycles(self) -> list[str]:
        """A derived metric that transitively depends on itself would recurse forever."""
        problems: list[str] = []

        def walk(key: str, seen: tuple[str, ...]) -> None:
            if key in seen:
                problems.append(f"metric cycle: {' -> '.join([*seen, key])}")
                return
            m = self.metric_map.get(key)
            if m is None:
                return
            deps: list[str] = []
            if m.kind == "ratio":
                deps = [d for d in (m.numerator, m.denominator) if d]
            elif m.kind == "derived":
                deps = self.derived_dependencies(m)
            for d in deps:
                walk(d, (*seen, key))

        for m in self.metrics:
            if m.kind in ("ratio", "derived"):
                walk(m.key, ())
        return sorted(set(problems))


# ---- errors ------------------------------------------------------------
# Every error carries the valid alternatives. An agent that gets
# "unknown metric 'rev'; did you mean: gross_revenue, net_revenue" can
# self-correct in one turn; "KeyError: rev" cannot.


class SemanticError(Exception):
    """Base for every semantic-layer failure."""


class UnknownMetric(SemanticError):
    def __init__(self, key: str, available: list[str]) -> None:
        self.key, self.available = key, available
        near = _nearest(key, available)
        hint = f"; did you mean: {', '.join(near)}" if near else ""
        super().__init__(f"unknown metric '{key}'{hint}")


class UnknownEntity(SemanticError):
    def __init__(self, key: str, available: list[str]) -> None:
        super().__init__(f"unknown entity '{key}'; available: {', '.join(available)}")


class UnknownDimension(SemanticError):
    def __init__(self, key: str, available: list[str]) -> None:
        self.key, self.available = key, available
        near = _nearest(key, available)
        hint = f"; did you mean: {', '.join(near)}" if near else f"; available: {', '.join(available[:12])}"
        super().__init__(f"unknown dimension '{key}'{hint}")


class AmbiguousDimension(SemanticError):
    def __init__(self, key: str, owners: str) -> None:
        super().__init__(f"dimension '{key}' is defined on multiple entities ({owners}); qualify it")


class MissingTimeColumn(SemanticError):
    def __init__(self, metric: str, entity: str) -> None:
        super().__init__(
            f"metric '{metric}' has no time column and entity '{entity}' has no "
            f"default_time_column, so it cannot be trended over time"
        )


def _nearest(key: str, options: list[str], n: int = 3) -> list[str]:
    import difflib

    bare = [o.split(".")[-1] for o in options]
    matches = difflib.get_close_matches(key.split(".")[-1], bare, n=n, cutoff=0.5)
    return [o for o in options if o.split(".")[-1] in matches][:n]
