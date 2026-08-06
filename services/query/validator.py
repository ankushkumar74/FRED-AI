"""AST-level SQL validation — BLUEPRINT 9.2 stage 9, 17.1 threat 3.

Regex validation of generated SQL is security theatre. Everything here works
on the parsed AST via SQLGlot, which is why it cannot be defeated by comments,
unicode, or nested statements.
"""
from __future__ import annotations
import sqlglot
from sqlglot import exp

FORBIDDEN = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
    exp.Alter, exp.Grant, exp.TruncateTable, exp.Merge,
)


class UnsafeSQL(Exception):
    pass


def validate(sql: str, dialect: str, *, allowed_tables: set[str],
             max_rows: int = 50_000) -> str:
    """Parse, assert read-only, assert allowlisted tables, inject LIMIT."""
    statements = sqlglot.parse(sql, read=dialect)
    if len(statements) != 1:
        raise UnsafeSQL(f"expected exactly one statement, got {len(statements)}")

    tree = statements[0]
    if tree is None:
        raise UnsafeSQL("failed to parse")

    for node_type in FORBIDDEN:
        if list(tree.find_all(node_type)):
            raise UnsafeSQL(f"forbidden statement type: {node_type.__name__}")

    referenced = {t.sql(dialect=dialect).lower() for t in tree.find_all(exp.Table)}
    if not referenced <= {t.lower() for t in allowed_tables}:
        raise UnsafeSQL(f"tables outside allowlist: {referenced - allowed_tables}")

    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(max_rows)

    return tree.sql(dialect=dialect)


def lineage(sql: str, dialect: str) -> dict[str, list[str]]:
    """Extract tables/columns for the audit log. Free, because we already parse."""
    tree = sqlglot.parse_one(sql, read=dialect)
    return {
        "tables": sorted({t.sql(dialect=dialect) for t in tree.find_all(exp.Table)}),
        "columns": sorted({c.sql(dialect=dialect) for c in tree.find_all(exp.Column)}),
    }
