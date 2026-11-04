"""Question in, answer out — the one orchestration path.

The CLI and the browser demo are both thin shells over `Ask.answer()`. Keeping a
single code path matters more than it looks: if the website ran a reimplemented
copy of the pipeline, the thing people try would not be the thing that is tested,
and the eval scores would describe software nobody is actually using.

What this does NOT do is explain *why* a metric moved. That needs the evidence
engine (BLUEPRINT Part 12), which is Phase 2. A diagnostic question gets the
trend plus an honest statement of the limit — never a manufactured cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from .query.executor import DuckDBEngine, Executor, QueryError
from .query.validator import UnsafeSQL
from .semantic.compiler import CompileError, Compiler
from .semantic.loader import load_file
from .semantic.models import Comparison, MetricRef, SemanticModel, TimeGrain, TimeRange
from .semantic.resolver import MetricResolver

#: The fixture's "today". A real deployment reads this from the connection's
#: freshness watermark rather than hardcoding it.
DEMO_TODAY = date(2026, 8, 2)

DIAGNOSTIC_WORDS = ("why", "cause", "caused", "driving", "drove", "explain", "reason", "dropped")

Status = Literal["answered", "clarify", "refused", "error"]


@dataclass
class Answer:
    question: str
    status: Status
    scope: str = ""
    metric_key: str | None = None
    metric_label: str | None = None
    metric_format: str = "number"
    certified: bool = False
    dimensions: list[str] = field(default_factory=list)
    grain: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    message: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    sql: str | None = None
    tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0
    from_cache: bool = False
    diagnostic: bool = False
    limitation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, for the browser bridge."""
        def encode(value: Any) -> Any:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            if isinstance(value, (int, float, str, bool)) or value is None:
                return value
            return str(value)

        return {
            "question": self.question,
            "status": self.status,
            "scope": self.scope,
            "metric_key": self.metric_key,
            "metric_label": self.metric_label,
            "metric_format": self.metric_format,
            "certified": self.certified,
            "dimensions": self.dimensions,
            "grain": self.grain,
            "columns": self.columns,
            "rows": [{k: encode(v) for k, v in row.items()} for row in self.rows],
            "message": self.message,
            "candidates": self.candidates,
            "sql": self.sql,
            "tables": self.tables,
            "notes": self.notes,
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
            "from_cache": self.from_cache,
            "diagnostic": self.diagnostic,
            "limitation": self.limitation,
        }


def parse_time_range(question: str, today: date = DEMO_TODAY) -> tuple[TimeRange, TimeGrain | None, str]:
    """Crude date-phrase parsing. Phase 1 scope — the planner owns this later."""
    q = question.lower()

    if "last week" in q or "past week" in q:
        end = today - timedelta(days=today.weekday() + 1)
        return TimeRange(start=end - timedelta(days=6), end=end), None, "last week"
    if "last month" in q or "past month" in q:
        end = today.replace(day=1) - timedelta(days=1)
        return TimeRange(start=end.replace(day=1), end=end), None, "last month"
    if "last quarter" in q:
        start = today.replace(day=1) - timedelta(days=90)
        return TimeRange(start=start.replace(day=1), end=today), None, "the last quarter"
    if "this year" in q or "ytd" in q:
        return TimeRange(start=today.replace(month=1, day=1), end=today), TimeGrain.MONTH, "year to date"
    if match := re.search(r"last (\d+) (day|week|month)s?", q):
        count, unit = int(match.group(1)), match.group(2)
        days = {"day": 1, "week": 7, "month": 30}[unit] * count
        grain = {"day": TimeGrain.DAY, "week": TimeGrain.WEEK, "month": TimeGrain.MONTH}[unit]
        return TimeRange(start=today - timedelta(days=days), end=today), grain, f"the last {count} {unit}s"

    end = today.replace(day=1) - timedelta(days=1)
    return TimeRange(start=end.replace(day=1), end=end), None, "last month"


def parse_grain(question: str) -> TimeGrain | None:
    q = question.lower()
    for phrase, grain in (
        ("daily", TimeGrain.DAY),
        ("weekly", TimeGrain.WEEK),
        ("monthly", TimeGrain.MONTH),
        ("by day", TimeGrain.DAY),
        ("by week", TimeGrain.WEEK),
        ("by month", TimeGrain.MONTH),
        ("trend", TimeGrain.WEEK),
        ("over time", TimeGrain.WEEK),
    ):
        if phrase in q:
            return grain
    return None


class Ask:
    """Holds the loaded model and an open connection; answers questions."""

    def __init__(
        self,
        metrics_path: str | Path,
        database: str | Path | None = None,
        *,
        engine: DuckDBEngine | None = None,
        today: date = DEMO_TODAY,
        dialect: str = "duckdb",
    ) -> None:
        """Pass `database` for a DuckDB file, or `engine` for a prepared connection.

        The injection seam exists because the browser demo builds its connection
        from Parquet, and because a warehouse connection will arrive the same way.
        """
        if engine is None:
            if database is None:
                raise ValueError("pass either `database` or `engine`")
            engine = DuckDBEngine(database)

        self.model: SemanticModel = load_file(metrics_path)
        self.resolver = MetricResolver(self.model)
        self.compiler = Compiler(self.model, dialect=dialect)
        self.executor = Executor(engine=engine, max_rows=100_000)
        self.today = today

    # ---- catalogue, for the UI ------------------------------------------

    def catalogue(self) -> list[dict[str, Any]]:
        return [
            {
                "key": m.key,
                "label": m.display,
                "kind": m.kind,
                "certified": m.certified,
                "description": (m.description or "").strip(),
                "synonyms": m.synonyms[:5],
            }
            for m in sorted(self.model.metrics, key=lambda m: m.display)
        ]

    # ---- the one entry point ---------------------------------------------

    def answer(self, question: str) -> Answer:
        question = question.strip()
        if not question:
            return Answer(question=question, status="clarify", message="Ask me about a metric.")

        resolution = self.resolver.resolve(question)
        if resolution.needs_clarification:
            return Answer(
                question=question,
                status="clarify",
                message=resolution.question,
                candidates=[
                    {"key": c.key, "label": c.metric.display, "score": round(c.score, 3),
                     "reasons": c.reasons}
                    for c in resolution.candidates[:4]
                ],
            )

        metric = resolution.metric
        assert metric is not None

        dimensions: list[str] = []
        if re.search(r"\b(by|per|across|split)\b", question, re.IGNORECASE):
            ranked = self.resolver.resolve_dimension(question)
            if ranked and ranked[0][1] >= 0.5:
                dimensions = [ranked[0][0].split(".")[-1]]

        window, inferred_grain, scope_label = parse_time_range(question, self.today)
        grain = parse_grain(question) or inferred_grain
        diagnostic = any(w in question.lower() for w in DIAGNOSTIC_WORDS)

        if diagnostic and not grain:
            grain = TimeGrain.WEEK
            window = TimeRange(start=self.today - timedelta(days=84), end=self.today)
            scope_label = "the last 12 weeks"

        ref = MetricRef(
            metric=metric.key,
            dimensions=dimensions,
            grain=grain,
            time_range=window,
            comparison=Comparison.NONE,
            order_by_value=bool(dimensions) and not grain,
        )

        try:
            compiled = self.compiler.compile(ref)
        except (CompileError, UnsafeSQL) as exc:
            return Answer(
                question=question,
                status="refused",
                metric_key=metric.key,
                metric_label=metric.display,
                message=str(exc),
            )

        try:
            result = self.executor.execute(compiled.sql)
        except (QueryError, UnsafeSQL) as exc:
            return Answer(
                question=question, status="error", metric_key=metric.key,
                metric_label=metric.display, message=str(exc), sql=compiled.sql,
            )

        scope = f"{metric.display} for {scope_label}"
        if dimensions:
            scope += f", by {dimensions[0]}"

        return Answer(
            question=question,
            status="answered",
            scope=scope,
            metric_key=metric.key,
            metric_label=metric.display,
            metric_format=metric.format.value,
            certified=metric.certified,
            dimensions=dimensions,
            grain=grain.value if grain else None,
            columns=result.columns,
            rows=result.rows()[:200],
            sql=compiled.sql,
            tables=result.tables_read,
            notes=compiled.notes,
            warnings=result.warnings,
            duration_ms=result.duration_ms,
            from_cache=result.from_cache,
            diagnostic=diagnostic,
            limitation=(
                "I can show what changed, but not yet why. Root cause analysis needs the "
                "evidence engine (blueprint Part 12), which is Phase 2. I would rather say "
                "that than invent an explanation."
            )
            if diagnostic
            else None,
        )
