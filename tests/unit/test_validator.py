"""SQL validation — BLUEPRINT 17.1 threat 3.

The parametrised attack matrix is the point of this file. Each payload defeats
regex-based validation and must not defeat an AST parser.
"""

from __future__ import annotations

import pytest

from services.query.validator import UnsafeSQL, lineage, validate

ALLOWED = {"fct_orders", "dim_customers", "fct_spend"}

BLOCKED = [
    ("statement stacking", "SELECT 1 FROM fct_orders; DROP TABLE fct_orders"),
    ("insert", "INSERT INTO fct_orders VALUES (1)"),
    ("update", "UPDATE fct_orders SET order_total = 0"),
    ("delete", "DELETE FROM fct_orders"),
    ("truncate", "TRUNCATE TABLE fct_orders"),
    ("drop", "DROP TABLE fct_orders"),
    ("create table as select", "CREATE TABLE exfil AS SELECT * FROM fct_orders"),
    ("alter", "ALTER TABLE fct_orders ADD COLUMN x INT"),
    ("delete inside cte", "WITH t AS (DELETE FROM fct_orders RETURNING *) SELECT * FROM t"),
    ("insert inside cte", "WITH t AS (INSERT INTO fct_orders VALUES (1) RETURNING *) SELECT * FROM t"),
    ("copy to file", "COPY fct_orders TO '/tmp/stolen.csv'"),
    ("read local file", "SELECT * FROM read_csv('/etc/passwd')"),
    ("read remote file", "SELECT * FROM read_parquet('https://evil.test/x.parquet')"),
    ("read_text projection", "SELECT read_text('/etc/passwd') AS leaked"),
    ("glob the filesystem", "SELECT * FROM glob('/**')"),
    ("install extension", "INSTALL httpfs"),
    ("attach another database", "ATTACH '/tmp/other.db' AS other"),
    ("set session config", "SET memory_limit = '100GB'"),
    ("table outside allowlist", "SELECT * FROM internal_secrets"),
]

ALLOWED_QUERIES = [
    ("plain select", "SELECT SUM(order_total) AS value FROM fct_orders"),
    ("comment containing ddl", "SELECT 1 AS v FROM fct_orders -- ; DROP TABLE fct_orders"),
    ("block comment with ddl", "SELECT 1 AS v FROM fct_orders /* DELETE FROM fct_orders */"),
    ("join", "SELECT c.segment FROM fct_orders o JOIN dim_customers c ON o.customer_id = c.customer_id"),
    ("cte", "WITH t AS (SELECT 1 AS v FROM fct_orders) SELECT * FROM t"),
    ("union all", "SELECT 1 AS v FROM fct_orders UNION ALL SELECT 2 AS v FROM fct_orders"),
    ("subquery", "SELECT (SELECT SUM(spend) FROM fct_spend) AS v FROM fct_orders"),
    ("window function", "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_id) FROM fct_orders"),
]


@pytest.mark.parametrize("name,sql", BLOCKED, ids=[n for n, _ in BLOCKED])
def test_blocked_with_allowlist(name: str, sql: str) -> None:
    with pytest.raises(UnsafeSQL):
        validate(sql, "duckdb", allowed_tables=ALLOWED)


@pytest.mark.parametrize("name,sql", BLOCKED, ids=[n for n, _ in BLOCKED])
def test_blocked_without_allowlist(name: str, sql: str) -> None:
    """The allowlist must not be the only thing standing between us and a file read.

    An earlier version passed the whole matrix *because* of the allowlist, and
    quietly allowed `read_csv('/etc/passwd')` when no allowlist was configured.
    """
    if name == "table outside allowlist":
        pytest.skip("this payload is only an allowlist violation by definition")
    with pytest.raises(UnsafeSQL):
        validate(sql, "duckdb", allowed_tables=None)


@pytest.mark.parametrize("name,sql", ALLOWED_QUERIES, ids=[n for n, _ in ALLOWED_QUERIES])
def test_legitimate_queries_pass(name: str, sql: str) -> None:
    result = validate(sql, "duckdb", allowed_tables=ALLOWED)
    assert result.sql


class TestLimits:
    def test_limit_injected_when_absent(self) -> None:
        result = validate("SELECT * FROM fct_orders", "duckdb", allowed_tables=ALLOWED, max_rows=500)
        assert result.limit_injected
        assert "500" in result.sql

    def test_tighter_existing_limit_is_respected(self) -> None:
        result = validate(
            "SELECT * FROM fct_orders LIMIT 10", "duckdb", allowed_tables=ALLOWED, max_rows=500
        )
        assert not result.limit_injected
        assert "10" in result.sql

    def test_excessive_limit_is_clamped(self) -> None:
        result = validate(
            "SELECT * FROM fct_orders LIMIT 999999", "duckdb", allowed_tables=ALLOWED, max_rows=500
        )
        assert result.limit_injected
        assert "999999" not in result.sql


class TestLineage:
    def test_physical_tables_reported(self) -> None:
        result = lineage(
            "SELECT o.order_total FROM fct_orders o JOIN dim_customers c "
            "ON o.customer_id = c.customer_id",
            "duckdb",
        )
        assert result["tables"] == ["dim_customers", "fct_orders"]

    def test_cte_names_are_not_tables(self) -> None:
        """A CTE is query-local. Listing it as a data source corrupts the audit log."""
        result = validate(
            "WITH numerator AS (SELECT 1 AS v FROM fct_orders) SELECT * FROM numerator",
            "duckdb",
            allowed_tables=ALLOWED,
        )
        assert result.tables == ["fct_orders"]


class TestErrors:
    def test_unparseable_sql_is_rejected(self) -> None:
        with pytest.raises(UnsafeSQL, match="could not parse"):
            validate("SELECT FROM WHERE", "duckdb", allowed_tables=ALLOWED)

    def test_all_reasons_are_reported(self) -> None:
        """An agent repairing a query should see every problem, not just the first."""
        with pytest.raises(UnsafeSQL) as exc:
            validate(
                "SELECT read_text('/etc/passwd') FROM internal_secrets",
                "duckdb",
                allowed_tables=ALLOWED,
            )
        assert len(exc.value.reasons) >= 2
