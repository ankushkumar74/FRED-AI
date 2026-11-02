#!/usr/bin/env python3
"""Golden-set evaluation — BLUEPRINT 19.4.

Traditional tests cannot validate a probabilistic system, and shipping a prompt
or threshold change without evals is how you wake up to a regression you cannot
explain. This harness gates every change to the semantic layer or the agents.

Four scorers, each answering a question you cannot answer with assertions:

  resolution_accuracy   did we bind the right metric from natural language?
  clarification_recall  did we ASK when the question was genuinely ambiguous?
  sql_execution_accuracy does compiled SQL return the same numbers as
                        independently hand-written reference SQL?
  refusal_correctness   did we refuse the queries that would produce wrong
                        numbers (fan-out, unshared dimensions)?

`sql_execution_accuracy` is the important one. The reference SQL in the dataset
is written by hand against the raw tables, so a compiler bug shows up as a value
mismatch instead of the compiler quietly agreeing with itself.

    python -m agents.evals.run --dataset tests/evals/golden.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.query.executor import DuckDBEngine, Executor  # noqa: E402
from services.semantic.compiler import Compiler  # noqa: E402
from services.semantic.loader import load_file  # noqa: E402
from services.semantic.models import MetricRef, TimeGrain, TimeRange  # noqa: E402
from services.semantic.resolver import MetricResolver  # noqa: E402

VALUE_TOLERANCE = 0.001  # 0.1% — covers decimal/float representation, nothing more


@dataclass
class CaseResult:
    case_id: str
    scorer: str
    passed: bool
    detail: str = ""


@dataclass
class Report:
    results: list[CaseResult] = field(default_factory=list)

    def add(self, case_id: str, scorer: str, passed: bool, detail: str = "") -> None:
        self.results.append(CaseResult(case_id, scorer, passed, detail))

    def scores(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for result in self.results:
            passed, total = out.get(result.scorer, (0, 0))
            out[result.scorer] = (passed + int(result.passed), total + 1)
        return out

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]


def _build_ref(spec: dict[str, Any]) -> MetricRef:
    return MetricRef(
        metric=spec["metric"],
        dimensions=spec.get("dimensions", []),
        grain=TimeGrain(spec["grain"]) if spec.get("grain") else None,
        time_range=TimeRange(
            start=date.fromisoformat(spec["start"]), end=date.fromisoformat(spec["end"])
        ),
    )


def _comparable(rows: list[dict[str, Any]]) -> list[tuple]:
    """Normalise a result set so two queries can be compared regardless of
    column naming, column order, or row order."""
    normalized: list[tuple] = []
    for row in rows:
        keys: list[str] = []
        value: float | None = None
        for name, raw in row.items():
            if name in ("value", "period_type"):
                if name == "value":
                    value = None if raw is None else float(raw)
                continue
            keys.append(str(raw))
        # Reference SQL names its measure `value` too, but be forgiving if the
        # only remaining column is clearly the measure.
        if value is None and len(row) == 1:
            value = None if next(iter(row.values())) is None else float(next(iter(row.values())))
        normalized.append((tuple(sorted(keys)), value))
    return sorted(normalized, key=lambda item: (item[0], item[1] is None))


def _values_match(a: list[tuple], b: list[tuple]) -> tuple[bool, str]:
    if len(a) != len(b):
        return False, f"row count {len(a)} vs reference {len(b)}"
    for (keys_a, value_a), (keys_b, value_b) in zip(a, b):
        if keys_a != keys_b:
            return False, f"group keys {keys_a} vs {keys_b}"
        if value_a is None or value_b is None:
            if value_a != value_b:
                return False, f"null mismatch at {keys_a}: {value_a} vs {value_b}"
            continue
        denominator = max(abs(value_a), abs(value_b), 1e-9)
        if abs(value_a - value_b) / denominator > VALUE_TOLERANCE:
            return False, f"value at {keys_a}: {value_a:,.4f} vs reference {value_b:,.4f}"
    return True, ""


def run_eval(dataset: Path, metrics_file: Path, database: Path) -> Report:
    model = load_file(metrics_file)
    resolver = MetricResolver(model)
    compiler = Compiler(model, dialect="duckdb")
    executor = Executor(engine=DuckDBEngine(database), max_rows=100_000)

    report = Report()
    cases = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]

    for case in cases:
        case_id = case["id"]
        question = case["question"]

        # --- resolution / clarification
        resolution = resolver.resolve(question)
        if case.get("expect_clarification"):
            report.add(
                case_id,
                "clarification_recall",
                resolution.needs_clarification,
                "" if resolution.needs_clarification else f"bound to {resolution.metric.key} instead of asking",  # type: ignore[union-attr]
            )
        elif case.get("expect_metric"):
            got = resolution.metric.key if resolution.metric else None
            passed = got == case["expect_metric"]
            report.add(
                case_id,
                "resolution_accuracy",
                passed,
                "" if passed else f"wanted {case['expect_metric']}, got {got or 'ASK(' + ','.join(resolution.top_keys(2)) + ')'}",
            )

        # --- dimension resolution
        for expected_dim in case.get("expect_dimensions", []):
            ranked = resolver.resolve_dimension(question)
            got_dims = [key.split(".")[-1] for key, _ in ranked]
            passed = expected_dim in got_dims[:1]
            report.add(
                case_id,
                "dimension_accuracy",
                passed,
                "" if passed else f"wanted {expected_dim}, got {got_dims[:3]}",
            )

        # --- refusals
        if case.get("expect_refusal"):
            try:
                compiler.compile(_build_ref(case["ref"]))
                report.add(case_id, "refusal_correctness", False, "compiled instead of refusing")
            except Exception as exc:
                passed = type(exc).__name__ == case["expect_refusal"]
                report.add(
                    case_id,
                    "refusal_correctness",
                    passed,
                    "" if passed else f"wanted {case['expect_refusal']}, got {type(exc).__name__}",
                )
            continue

        # --- value fidelity against hand-written reference SQL
        if case.get("expect_sql") and case.get("ref"):
            try:
                compiled = compiler.compile(_build_ref(case["ref"]))
                actual = _comparable(executor.execute(compiled.sql).rows())
                expected = _comparable(executor.execute(case["expect_sql"]).rows())
                passed, detail = _values_match(actual, expected)
                report.add(case_id, "sql_execution_accuracy", passed, detail)
            except Exception as exc:
                report.add(
                    case_id, "sql_execution_accuracy", False, f"{type(exc).__name__}: {exc}"
                )

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="tests/evals/golden.jsonl")
    parser.add_argument("--metrics", default="configs/metrics/ecommerce.yaml")
    parser.add_argument("--database", default="demo.duckdb")
    parser.add_argument("--gates", default="configs/eval_gates.yaml")
    parser.add_argument("--no-gate", action="store_true", help="report without failing the build")
    args = parser.parse_args()

    database = Path(args.database)
    if not database.exists():
        print(f"fixture {database} not found — run: python scripts/seed_demo.py --out {database}")
        return 2

    report = run_eval(Path(args.dataset), Path(args.metrics), database)

    gates: dict[str, float] = {}
    gates_path = Path(args.gates)
    if gates_path.exists():
        import yaml

        gates = {k: v for k, v in (yaml.safe_load(gates_path.read_text()) or {}).items()
                 if isinstance(v, (int, float))}

    print(f"\n{'scorer':<26} {'score':>10} {'gate':>8}  result")
    print("-" * 60)
    failed_gates: list[str] = []
    for scorer, (passed, total) in sorted(report.scores().items()):
        ratio = passed / total if total else 1.0
        gate = gates.get(scorer)
        status = "ok"
        if gate is not None and ratio < gate:
            status = "BELOW GATE"
            failed_gates.append(scorer)
        gate_text = f"{gate:.0%}" if gate is not None else "-"
        print(f"{scorer:<26} {passed:>4}/{total:<5} {ratio:>6.0%} {gate_text:>8}  {status}")

    if report.failures():
        print(f"\n{len(report.failures())} failing case(s):")
        for failure in report.failures():
            print(f"  [{failure.scorer}] {failure.case_id}: {failure.detail}")

    if failed_gates and not args.no_gate:
        print(f"\nFAILED: {', '.join(failed_gates)} below gate")
        return 1
    print("\nall gates passed" if not failed_gates else "\ngates ignored (--no-gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
