"""Query execution — validated, bounded, cached, and content-addressed.

Every query goes through `validate()` first, including queries produced by our
own compiler. The compiler is trusted; "trusted" is still verified, because the
parse was happening anyway for lineage and the marginal cost is zero.

Results are Arrow tables rather than lists of dicts or DataFrames. Arrow is the
wire format end-to-end (BLUEPRINT 4.3): zero-copy into DuckDB for cross-source
joins, cheap to serialise to Parquet for the result store, and typed, which
matters when a metric is a DECIMAL and must not silently become a float.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pyarrow as pa

from .validator import UnsafeSQL, ValidationResult, validate

DEFAULT_TIMEOUT_S = 30
DEFAULT_MAX_ROWS = 50_000


class QueryError(Exception):
    """Execution failed. Carries the SQL so the repair loop has something to work with."""

    def __init__(self, message: str, *, sql: str | None = None, dialect: str | None = None) -> None:
        self.sql, self.dialect = sql, dialect
        super().__init__(message)


class QueryTimeout(QueryError):
    pass


@dataclass
class ResultSet:
    table: pa.Table
    sql: str
    dialect: str
    duration_ms: int
    from_cache: bool = False
    tables_read: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return int(self.table.num_rows)

    @property
    def columns(self) -> list[str]:
        return list(self.table.column_names)

    @property
    def schema(self) -> dict[str, str]:
        return {f.name: str(f.type) for f in self.table.schema}

    @property
    def content_hash(self) -> str:
        """Stable hash of the data itself, for dedup across investigations.

        Two different queries that return the same numbers produce the same hash,
        which is what makes "we already have this evidence" answerable.
        """
        digest = hashlib.sha256()
        for batch in self.table.to_batches():
            for column in batch.columns:
                digest.update(str(column.to_pylist()).encode())
        return digest.hexdigest()[:32]

    def rows(self) -> list[dict[str, Any]]:
        return list(self.table.to_pylist())

    def sample_rows(self, n: int = 5) -> list[dict[str, Any]]:
        return list(self.table.slice(0, n).to_pylist())

    def scalar(self) -> Any:
        """The single value of a 1x1 result, or None when the result is empty.

        Raises when the shape is wrong rather than silently returning the first
        cell of something larger — a lookup that unexpectedly returns 40 rows is
        a bug worth surfacing, not rounding off.
        """
        if self.table.num_rows == 0:
            return None
        if self.table.num_columns != 1 or self.table.num_rows != 1:
            raise QueryError(
                f"expected a single value, got {self.table.num_rows} rows x "
                f"{self.table.num_columns} columns",
                sql=self.sql,
            )
        return self.table.column(0)[0].as_py()

    def column_values(self, name: str) -> list[Any]:
        return list(self.table.column(name).to_pylist())


def _is_internal(table_name: str) -> bool:
    """Underscore-prefixed tables are ours, not the customer's, and are never queryable.

    The immediate case is `_ground_truth` in the test fixture: if the system under
    test can read the answer key, every eval score is meaningless. The general case
    is any bookkeeping table we materialise inside a customer's database.
    """
    return table_name.rsplit(".", 1)[-1].startswith("_")


class QueryEngine(ABC):
    """One per warehouse type. Phase 1 ships DuckDB and Postgres."""

    dialect: str

    @abstractmethod
    def run(self, sql: str, timeout_s: int) -> pa.Table: ...

    @abstractmethod
    def list_tables(self) -> set[str]: ...

    def close(self) -> None:  # pragma: no cover - trivial default
        return None


class DuckDBEngine(QueryEngine):
    """Local analytical engine. Powers file uploads, cross-source joins and demos.

    Opened read-only: a validator bug should still not be able to write, because
    one layer of defence is not defence.
    """

    dialect = "duckdb"

    def __init__(self, database: str | Path = ":memory:", *, read_only: bool = True) -> None:
        import duckdb

        self.database = str(database)
        in_memory = self.database == ":memory:"
        self._con = duckdb.connect(self.database, read_only=read_only and not in_memory)

    def run(self, sql: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> pa.Table:
        import duckdb

        try:
            # `con.sql(...).to_arrow_table()` rather than `con.execute(...)`:
            # going through the DB-API result copies every row an extra time, and
            # DuckDB warns about it. The relation path hands back Arrow directly.
            #
            # Never `.arrow()` either — newer DuckDB returns a RecordBatchReader
            # from it, which is single-pass and so cannot be cached or re-read.
            # We need a materialised Table.
            relation = self._con.sql(sql)
            if relation is None:  # pragma: no cover - validator rejects non-SELECT
                return pa.table({})
            return cast("pa.Table", relation.to_arrow_table())
        except duckdb.Error as exc:
            raise QueryError(str(exc), sql=sql, dialect=self.dialect) from exc

    def list_tables(self) -> set[str]:
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema NOT IN "
            "('information_schema', 'pg_catalog')"
        ).fetchall()
        return {r[0] for r in rows if not _is_internal(r[0])}

    def close(self) -> None:
        self._con.close()


class PostgresEngine(QueryEngine):
    """Federated access to a customer's Postgres.

    Three protections the blueprint calls for are applied per session rather than
    hoped for: a read-only transaction, a statement timeout the server enforces
    (so a hung query cannot outlive our client), and no implicit commits.
    """

    dialect = "postgres"

    def __init__(self, dsn: str, *, application_name: str = "fred") -> None:
        self.dsn = dsn
        self.application_name = application_name

    def run(self, sql: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> pa.Table:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency is optional in Phase 1
            raise QueryError(
                "psycopg is not installed; add it to run against Postgres", sql=sql
            ) from exc

        try:
            with psycopg.connect(
                self.dsn, application_name=self.application_name, autocommit=False
            ) as con:
                with con.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
                    cur.execute(sql)
                    columns = [d.name for d in (cur.description or [])]
                    rows = cur.fetchall()
                con.rollback()
        except Exception as exc:
            message = str(exc)
            if "statement timeout" in message.lower():
                raise QueryTimeout(message, sql=sql, dialect=self.dialect) from exc
            raise QueryError(message, sql=sql, dialect=self.dialect) from exc

        if not columns:
            return pa.table({})
        return pa.table({name: [row[i] for row in rows] for i, name in enumerate(columns)})

    def list_tables(self) -> set[str]:
        table = self.run(
            "SELECT table_schema || '.' || table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')",
            timeout_s=10,
        )
        if not table.num_columns:
            return set()
        return {name for name in table.column(0).to_pylist() if not _is_internal(name)}


class ResultCache:
    """In-process result cache keyed by (sql, dialect, freshness bucket).

    Phase 1 is a dict; Phase 3 is Redis behind the same two methods. The
    freshness bucket is part of the key so a cached answer expires when the
    underlying connection's watermark moves, rather than on a blind TTL —
    a stale number presented as current is the failure mode to avoid.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._entries: dict[str, tuple[pa.Table, list[str]]] = {}
        self._max_entries = max_entries
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(sql: str, dialect: str, freshness: str | None) -> str:
        payload = json.dumps({"sql": sql, "dialect": dialect, "freshness": freshness}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, key: str) -> tuple[pa.Table, list[str]] | None:
        hit = self._entries.get(key)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, key: str, table: pa.Table, tables_read: list[str]) -> None:
        if len(self._entries) >= self._max_entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (table, tables_read)

    def clear(self) -> None:
        self._entries.clear()


@dataclass
class Executor:
    """Validate, cache, execute, measure.

    `allowed_tables=None` means "discover from the engine" — the allowlist is
    then the set of tables that actually exist, which still blocks references to
    anything invented by a model.
    """

    engine: QueryEngine
    cache: ResultCache | None = None
    max_rows: int = DEFAULT_MAX_ROWS
    timeout_s: int = DEFAULT_TIMEOUT_S
    allowed_tables: set[str] | None = None
    _discovered: set[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = ResultCache()

    def allowlist(self) -> set[str]:
        if self.allowed_tables is not None:
            return self.allowed_tables
        if self._discovered is None:
            self._discovered = self.engine.list_tables()
        return self._discovered

    def check(self, sql: str) -> ValidationResult:
        """Validate without executing. Used by the SQL agent's repair loop."""
        return validate(
            sql,
            self.engine.dialect,
            allowed_tables=self.allowlist(),
            max_rows=self.max_rows,
        )

    def execute(self, sql: str, *, freshness: str | None = None) -> ResultSet:
        validation = self.check(sql)
        assert self.cache is not None

        cache_key = ResultCache.key(validation.sql, self.engine.dialect, freshness)
        cached = self.cache.get(cache_key)
        if cached is not None:
            table, tables_read = cached
            return ResultSet(
                table=table,
                sql=validation.sql,
                dialect=self.engine.dialect,
                duration_ms=0,
                from_cache=True,
                tables_read=tables_read,
                warnings=validation.warnings,
            )

        started = time.perf_counter()
        table = self.engine.run(validation.sql, self.timeout_s)
        duration_ms = int((time.perf_counter() - started) * 1000)

        self.cache.put(cache_key, table, validation.tables)
        return ResultSet(
            table=table,
            sql=validation.sql,
            dialect=self.engine.dialect,
            duration_ms=duration_ms,
            tables_read=validation.tables,
            warnings=validation.warnings,
            truncated=validation.limit_injected and table.num_rows >= self.max_rows,
        )

    def explain(self, sql: str) -> dict[str, Any]:
        """Pre-execution metadata for the trace, without running anything."""
        try:
            validation = self.check(sql)
        except UnsafeSQL as exc:
            return {"valid": False, "reasons": exc.reasons}
        return {
            "valid": True,
            "sql": validation.sql,
            "tables": validation.tables,
            "columns": validation.columns,
            "warnings": validation.warnings,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
