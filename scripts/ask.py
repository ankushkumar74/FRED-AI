#!/usr/bin/env python3
"""Ask a question end to end: natural language -> metric -> SQL -> answer.

The Phase 1 slice, runnable with no services:

    python scripts/seed_demo.py --out demo.duckdb
    python scripts/ask.py "revenue by channel last month"
    python scripts/ask.py "what is our conversion rate"     # declines, on purpose
    python scripts/ask.py "revenue by product category"     # refuses, on purpose

Every answer prints its trace. That is not a debug flag — it is the trust
mechanism (BLUEPRINT 1.2, principle 2), and an analyst who cannot see the SQL
will not let this near their CEO.

What this does NOT do yet: explain *why* anything changed. Diagnostics need the
evidence engine (Part 12), which is Phase 2. A "why" question is answered with
the relevant trend and an honest statement of that limit, rather than a
plausible-sounding story.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.query.executor import DuckDBEngine, Executor
from services.query.validator import UnsafeSQL
from services.semantic.compiler import CompileError, Compiler
from services.semantic.loader import load_file
from services.semantic.models import Comparison, MetricRef, TimeGrain, TimeRange
from services.semantic.resolver import MetricResolver

#: The fixture's "today". Real deployments read this from the connection watermark.
TODAY = date(2026, 8, 2)

DIAGNOSTIC_WORDS = ("why", "cause", "caused", "driving", "drove", "explain", "reason")


def parse_time_range(question: str) -> tuple[TimeRange, TimeGrain | None, str]:
    """Crude date-phrase parsing. Phase 1 scope — the planner owns this later."""
    q = question.lower()

    if "last week" in q or "past week" in q:
        end = TODAY - timedelta(days=TODAY.weekday() + 1)
        start = end - timedelta(days=6)
        return TimeRange(start=start, end=end), None, "last week"
    if "last month" in q or "past month" in q:
        first_this = TODAY.replace(day=1)
        end = first_this - timedelta(days=1)
        return TimeRange(start=end.replace(day=1), end=end), None, "last month"
    if "last quarter" in q:
        start = TODAY.replace(day=1) - timedelta(days=90)
        return TimeRange(start=start.replace(day=1), end=TODAY), None, "the last quarter"
    if "this year" in q or "ytd" in q:
        return TimeRange(start=TODAY.replace(month=1, day=1), end=TODAY), TimeGrain.MONTH, "year to date"
    if match := re.search(r"last (\d+) (day|week|month)s?", q):
        count, unit = int(match.group(1)), match.group(2)
        days = {"day": 1, "week": 7, "month": 30}[unit] * count
        grain = {"day": TimeGrain.DAY, "week": TimeGrain.WEEK, "month": TimeGrain.MONTH}[unit]
        return TimeRange(start=TODAY - timedelta(days=days), end=TODAY), grain, f"the last {count} {unit}s"

    start = TODAY.replace(day=1) - timedelta(days=1)
    return TimeRange(start=start.replace(day=1), end=start), None, "last month"


def parse_grain(question: str) -> TimeGrain | None:
    q = question.lower()
    for word, grain in (
        ("daily", TimeGrain.DAY),
        ("weekly", TimeGrain.WEEK),
        ("monthly", TimeGrain.MONTH),
        ("by day", TimeGrain.DAY),
        ("by week", TimeGrain.WEEK),
        ("by month", TimeGrain.MONTH),
        ("trend", TimeGrain.WEEK),
        ("over time", TimeGrain.WEEK),
    ):
        if word in q:
            return grain
    return None


def format_value(value: object, fmt: str) -> str:
    if value is None:
        return "n/a"
    number = float(value)  # type: ignore[arg-type]
    if fmt == "currency":
        return f"${number:,.2f}"
    if fmt == "percent":
        return f"{number * 100:,.2f}%"
    return f"{number:,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+")
    parser.add_argument("--database", default="demo.duckdb")
    parser.add_argument("--metrics", default="configs/metrics/ecommerce.yaml")
    parser.add_argument("--trace", action="store_true", help="print the SQL and lineage")
    args = parser.parse_args()

    question = " ".join(args.question)
    database = Path(args.database)
    if not database.exists():
        print(f"fixture {database} not found. Run: python scripts/seed_demo.py --out {database}")
        return 2

    model = load_file(Path(args.metrics))
    resolver = MetricResolver(model)
    compiler = Compiler(model, dialect="duckdb")
    executor = Executor(engine=DuckDBEngine(database), max_rows=100_000)

    print(f"\n  Q: {question}\n")

    # --- resolve, or ask ------------------------------------------------
    resolution = resolver.resolve(question)
    if resolution.needs_clarification:
        print(f"  {resolution.question}\n")
        if resolution.candidates:
            print("  closest candidates:")
            for candidate in resolution.candidates[:3]:
                print(f"    {candidate.key:<18} score={candidate.score:.2f} {candidate.reasons}")
        print()
        return 0

    metric = resolution.metric
    assert metric is not None

    dimensions: list[str] = []
    if re.search(r"\b(by|per|across|split)\b", question, re.IGNORECASE):
        ranked = resolver.resolve_dimension(question)
        if ranked and ranked[0][1] >= 0.5:
            dimensions = [ranked[0][0].split(".")[-1]]

    window, inferred_grain, window_label = parse_time_range(question)
    grain = parse_grain(question) or inferred_grain
    diagnostic = any(word in question.lower() for word in DIAGNOSTIC_WORDS)
    if diagnostic and not grain:
        grain = TimeGrain.WEEK
        window = TimeRange(start=TODAY - timedelta(days=84), end=TODAY)
        window_label = "the last 12 weeks"

    ref = MetricRef(
        metric=metric.key,
        dimensions=dimensions,
        grain=grain,
        time_range=window,
        comparison=Comparison.PRIOR_PERIOD if (diagnostic and not grain) else Comparison.NONE,
        order_by_value=bool(dimensions) and not grain,
    )

    # --- compile, or refuse ----------------------------------------------
    try:
        compiled = compiler.compile(ref)
    except (CompileError, UnsafeSQL) as exc:
        print(f"  I can't answer that correctly:\n    {exc}\n")
        return 0

    result = executor.execute(compiled.sql)

    # --- present -----------------------------------------------------------
    scope = f"{metric.display} for {window_label}"
    if dimensions:
        scope += f", by {dimensions[0]}"
    print(f"  {scope}")
    print(f"  {'-' * len(scope)}")

    rows = result.rows()
    if not rows:
        print("  no data in that window\n")
        return 0

    if len(rows) == 1 and len(result.columns) == 1:
        print(f"  {format_value(rows[0]['value'], metric.format.value)}")
    else:
        label_columns = [c for c in result.columns if c not in ("value", "period_type")]
        for row in rows[:20]:
            label = " / ".join(
                str(row[c].date() if hasattr(row[c], "date") else row[c]) for c in label_columns
            )
            print(f"  {label:<28} {format_value(row['value'], metric.format.value):>16}")
        if len(rows) > 20:
            print(f"  ... {len(rows) - 20} more rows")

    if diagnostic:
        print(
            "\n  Note: I can show what changed, but not yet why. Root cause analysis\n"
            "  needs the evidence engine (BLUEPRINT Part 12), which is Phase 2. I'd\n"
            "  rather say that than invent an explanation."
        )

    print(
        f"\n  [{result.duration_ms}ms"
        f"{', cached' if result.from_cache else ''}"
        f" | {result.row_count} rows | {', '.join(result.tables_read)}"
        f" | metric {'certified' if metric.certified else 'uncertified'}]"
    )
    for note in compiled.notes:
        print(f"  [note] {note}")
    for warning in result.warnings:
        print(f"  [warn] {warning}")

    if args.trace:
        print("\n  --- SQL " + "-" * 50)
        for line in compiled.sql.splitlines():
            print(f"  {line}")
        print(f"\n  columns read: {', '.join(compiled.columns)}")
    else:
        print("  (--trace to see the SQL)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
