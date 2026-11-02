"""Load and validate semantic layer definitions from YAML.

Metric definitions live in version control, not a database, because a change to
the definition of revenue is a change to every number the company reports and
belongs in a diff with a reviewer's name on it.

Validation is deliberately strict and fails with every problem at once rather
than the first — an analyst fixing a metric pack wants the whole list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    Dimension,
    Entity,
    Join,
    Metric,
    SemanticError,
    SemanticModel,
    TimeIntelligence,
)


class SemanticLoadError(SemanticError):
    def __init__(self, problems: list[str], source: str | None = None) -> None:
        self.problems = problems
        where = f" in {source}" if source else ""
        listed = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{len(problems)} problem(s){where}:\n{listed}")


def load_file(path: str | Path) -> SemanticModel:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return load_dict(raw, source=str(path))


def load_directory(directory: str | Path, pattern: str = "*.y*ml") -> SemanticModel:
    """Merge every metric pack in a directory into one model.

    Packs are additive; a duplicate metric key across two files is an error
    rather than a last-one-wins surprise.
    """
    directory = Path(directory)
    files = sorted(directory.glob(pattern))
    if not files:
        raise SemanticLoadError([f"no semantic definition files matching '{pattern}'"], str(directory))

    merged: dict[str, Any] = {"entities": [], "joins": [], "metrics": []}
    time_intelligence: dict[str, Any] | None = None
    problems: list[str] = []
    seen: dict[str, dict[str, str]] = {"entities": {}, "metrics": {}}

    for file in files:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for section in ("entities", "joins", "metrics"):
            for item in raw.get(section) or []:
                key = item.get("key")
                if section in seen and key:
                    if key in seen[section]:
                        problems.append(
                            f"duplicate {section[:-1]} '{key}' in {file.name} "
                            f"(already defined in {seen[section][key]})"
                        )
                        continue
                    seen[section][key] = file.name
                merged[section].append(item)
        if raw.get("time_intelligence"):
            time_intelligence = raw["time_intelligence"]

    if time_intelligence:
        merged["time_intelligence"] = time_intelligence
    if problems:
        raise SemanticLoadError(problems, str(directory))
    return load_dict(merged, source=str(directory))


def load_dict(raw: dict[str, Any], source: str | None = None) -> SemanticModel:
    problems: list[str] = []

    entities: list[Entity] = []
    for item in raw.get("entities") or []:
        try:
            entities.append(_parse_entity(item))
        except Exception as exc:
            problems.append(f"entity '{item.get('key', '?')}': {exc}")

    joins: list[Join] = []
    for item in raw.get("joins") or []:
        try:
            joins.append(Join(**_normalize_join_keys(item)))
        except Exception as exc:
            problems.append(f"join {item.get('left', '?')}->{item.get('right', '?')}: {_terse(exc)}")

    metrics: list[Metric] = []
    for item in raw.get("metrics") or []:
        try:
            metrics.append(_parse_metric(item))
        except Exception as exc:
            problems.append(f"metric '{item.get('key', '?')}': {_terse(exc)}")

    try:
        ti = TimeIntelligence(**(raw.get("time_intelligence") or {}))
    except Exception as exc:
        problems.append(f"time_intelligence: {_terse(exc)}")
        ti = TimeIntelligence()

    if problems:
        raise SemanticLoadError(problems, source)

    model = SemanticModel(
        version=raw.get("version", 1),
        entities=entities,
        joins=joins,
        metrics=metrics,
        time_intelligence=ti,
    )

    graph_problems = model.validate_graph()
    if graph_problems:
        raise SemanticLoadError(graph_problems, source)

    return model


def _normalize_join_keys(item: dict[Any, Any]) -> dict[str, Any]:
    """Work around YAML 1.1 treating a bare `on:` key as the boolean True.

    `on`, `off`, `yes` and `no` are all booleans in YAML 1.1, which PyYAML
    implements. A join written the obvious way:

        - left: orders
          right: customers
          on: "orders.customer_id = customers.customer_id"

    arrives as `{True: '...'}`. Quoting the key fixes it, but no analyst should
    have to know that, so the loader repairs it instead of rejecting the pack.
    """
    out: dict[str, Any] = {}
    for key, value in item.items():
        if key is True:
            out["on"] = value
        elif key is False:
            out["off"] = value
        else:
            out[str(key)] = value
    return out


def _parse_entity(item: dict[str, Any]) -> Entity:
    data = dict(item)
    dims = []
    for raw_dim in data.pop("dimensions", None) or []:
        if isinstance(raw_dim, str):
            dims.append(Dimension(key=raw_dim))
        else:
            dims.append(Dimension(**raw_dim))

    time_col = data.get("default_time_column")
    if time_col and not any(d.key == time_col for d in dims):
        # The time column is usable as a dimension without being declared twice.
        dims.append(Dimension(key=time_col, is_time=True, data_type="timestamp"))

    return Entity(**data, dimensions=dims)


def _parse_metric(item: dict[str, Any]) -> Metric:
    data = dict(item)

    # Accept `type:` as an alias for `kind:` — the blueprint's YAML examples use
    # `type`, and breaking a customer's metric pack over a keyword is petty.
    kind = data.pop("type", None) or data.pop("kind", None)
    if kind is None:
        if data.get("numerator") or data.get("denominator"):
            kind = "ratio"
        elif data.get("expression"):
            kind = "derived"
        else:
            kind = "simple"
    data["kind"] = kind

    if isinstance(data.get("filters"), str):
        data["filters"] = [data["filters"]]

    return Metric(**data)


def _terse(exc: Exception) -> str:
    """Pydantic's multi-line validation errors are unreadable in a problem list."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in exc.errors()
        )
    return str(exc)
