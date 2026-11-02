"""AST-level SQL validation — BLUEPRINT 9.2 stage 9, 17.1 threat 3.

Regex validation of generated SQL is security theatre. `'; DROP TABLE users--`
hidden in a comment, a unicode homoglyph, a nested CTE, a second statement after
a semicolon: all of these defeat pattern matching and none of them defeat a
parser. Everything here operates on the SQLGlot parse tree.

This runs on *every* query regardless of provenance — compiler output included.
The compiler is trusted, but "trusted" is a property we verify rather than
assume, and the cost of verifying is one parse we were doing anyway for lineage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

#: Node types that mutate data or schema. Presence of any is an immediate reject.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Set,
    exp.Command,  # COPY, VACUUM, CALL, and anything SQLGlot parses as a bare command
)

#: Functions that read or write the filesystem / network from inside SQL.
#: DuckDB in particular will happily read a local file or an https URL mid-query.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_text",
        "read_blob",
        "copy",
        "install",
        "load",
        "glob",
        "pg_read_file",
        "pg_read_binary_file",
        "lo_import",
        "lo_export",
        "dblink",
        "query",
        "sniff_csv",
        "parquet_scan",
    }
)

DEFAULT_MAX_ROWS = 50_000


class UnsafeSQL(Exception):
    """The query must not be executed. Carries every reason, not just the first."""

    def __init__(self, reasons: list[str], sql: str | None = None) -> None:
        self.reasons = reasons
        self.sql = sql
        super().__init__("; ".join(reasons))


@dataclass
class ValidationResult:
    sql: str
    dialect: str
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    limit_injected: bool = False
    warnings: list[str] = field(default_factory=list)


def validate(
    sql: str,
    dialect: str,
    *,
    allowed_tables: set[str] | None = None,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    require_time_filter: bool = False,
) -> ValidationResult:
    """Parse, prove read-only, check the table allowlist, and bound the result.

    Returns the rewritten SQL. Raises `UnsafeSQL` with every reason found.
    """
    reasons: list[str] = []
    warnings: list[str] = []

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as exc:
        raise UnsafeSQL([f"could not parse as {dialect}: {exc}"], sql) from exc

    if not statements:
        raise UnsafeSQL(["empty statement"], sql)
    if len(statements) > 1:
        # Statement stacking is the classic injection payload shape.
        raise UnsafeSQL(
            [f"expected exactly one statement, got {len(statements)} — statement stacking is rejected"],
            sql,
        )

    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery, exp.With)):
        raise UnsafeSQL([f"only SELECT queries may be executed, got {type(tree).__name__}"], sql)

    for node_type in FORBIDDEN_NODES:
        if isinstance(tree, node_type) or any(True for _ in tree.find_all(node_type)):
            reasons.append(f"contains a {node_type.__name__.upper()} node; queries must be read-only")

    reasons.extend(_forbidden_function_calls(tree))
    reasons.extend(_table_valued_functions(tree, dialect))

    tables = _referenced_tables(tree, dialect)

    if allowed_tables is not None:
        permitted = {t.lower() for t in allowed_tables}
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        offending = sorted(
            t for t in tables if t.lower() not in permitted and t.lower() not in cte_names
        )
        if offending:
            reasons.append(
                f"references tables outside the allowlist: {', '.join(offending)}"
            )

    if reasons:
        raise UnsafeSQL(reasons, sql)

    limit_injected = False
    if max_rows is not None:
        tree, limit_injected = _apply_limit(tree, max_rows)
        if limit_injected:
            warnings.append(f"LIMIT {max_rows} injected")

    if require_time_filter and not _has_comparison_predicate(tree):
        warnings.append("no WHERE predicate found; this query scans the full table")

    return ValidationResult(
        sql=tree.sql(dialect=dialect, pretty=True),
        dialect=dialect,
        tables=tables,
        columns=sorted({c.name for c in tree.find_all(exp.Column) if c.name}),
        limit_injected=limit_injected,
        warnings=warnings,
    )


def lineage(sql: str, dialect: str) -> dict[str, list[str]]:
    """Tables and columns a query touches, for the audit log.

    Free, because validation already parses. BLUEPRINT 6.3 stores this as
    `queries.ast_lineage` so every answer can show what it read.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    return {
        "tables": _referenced_tables(tree, dialect),
        "columns": sorted({c.sql(dialect=dialect) for c in tree.find_all(exp.Column) if c.name}),
    }


def _normalize(name: str) -> str:
    return name.lower().replace("_", "")


_FORBIDDEN_NORMALIZED = frozenset(_normalize(f) for f in FORBIDDEN_FUNCTIONS)


def _forbidden_function_calls(tree: exp.Expression) -> list[str]:
    """Catch forbidden functions however SQLGlot chose to represent them.

    The same call can arrive as `Anonymous(this='read_csv')` in a projection or
    as a dedicated `ReadCSV` node in a FROM clause, and the class name drops the
    underscore. Normalising both sides avoids a blocklist that only works in
    some syntactic positions.
    """
    found: set[str] = set()
    for node in tree.find_all(exp.Func):
        for candidate in (type(node).__name__, getattr(node, "name", "") or ""):
            if candidate and _normalize(candidate) in _FORBIDDEN_NORMALIZED:
                found.add(candidate.lower())
    return [f"calls '{name}', which can reach the filesystem or network" for name in sorted(found)]


def _table_valued_functions(tree: exp.Expression, dialect: str) -> list[str]:
    """Reject anything in table position that is not a plain identifier.

    This is a shape rule, not a name rule, and that is the point: DuckDB keeps
    adding table functions that read files and URLs, and a blocklist is always
    one release behind. A legitimate table reference is an identifier, so
    `FROM read_csv(...)`, `FROM glob(...)` and whatever ships next are all
    rejected by construction.
    """
    reasons: list[str] = []
    for table in tree.find_all(exp.Table):
        inner = table.this
        if inner is None or isinstance(inner, exp.Identifier):
            continue
        reasons.append(
            f"table position contains {type(inner).__name__} rather than a table name "
            f"({table.sql(dialect=dialect)[:60]}); table-valued functions are not executable here"
        )
    return reasons


def _referenced_tables(tree: exp.Expression, dialect: str) -> list[str]:
    """Physical tables only — CTE names are query-local, not data sources."""
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    names: set[str] = set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        parts = [p.name for p in (table.args.get("catalog"), table.args.get("db"), table.this) if p]
        if parts and ".".join(parts).lower() not in cte_names:
            names.add(".".join(parts))
    return sorted(names)


def _apply_limit(tree: exp.Expression, max_rows: int) -> tuple[exp.Expression, bool]:
    """Bound the result set, tightening an existing LIMIT rather than loosening it.

    A query that already asks for 10 rows keeps asking for 10. A query asking for
    a million gets clamped. A query with no limit gets one.
    """
    existing = tree.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.name)
        except (AttributeError, ValueError):
            return tree, False
        if current <= max_rows:
            return tree, False
        return tree.limit(max_rows), True

    if isinstance(tree, (exp.Select, exp.Union, exp.With)):
        return tree.limit(max_rows), True
    return tree, False


def _has_comparison_predicate(tree: exp.Expression) -> bool:
    for where in tree.find_all(exp.Where):
        if any(True for _ in where.find_all(exp.Binary)):
            return True
    return False
