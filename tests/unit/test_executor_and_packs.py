"""Executor behaviour and metric-pack loading.

Mostly failure paths. The happy path is covered by the integration suite; what
is worth pinning here is what happens when a query is malformed, a result is the
wrong shape, or a metric pack is broken — because those are the paths that run
on a bad day and the ones nobody exercises by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.query.executor import (
    DuckDBEngine,
    Executor,
    QueryError,
    ResultCache,
    _is_internal,
)
from services.query.validator import UnsafeSQL
from services.semantic.loader import SemanticLoadError, load_directory, load_file


@pytest.fixture
def engine() -> DuckDBEngine:
    """In-memory DuckDB with a couple of tables, built without touching disk."""
    eng = DuckDBEngine(":memory:", read_only=False)
    eng._con.execute("CREATE TABLE fct_orders (id INTEGER, amount DECIMAL(10,2), channel VARCHAR)")
    eng._con.execute(
        "INSERT INTO fct_orders VALUES (1, 10.50, 'Paid'), (2, 20.25, 'Organic'), (3, 5.00, 'Paid')"
    )
    eng._con.execute("CREATE TABLE _internal_audit (id INTEGER)")
    return eng


@pytest.fixture
def executor(engine: DuckDBEngine) -> Executor:
    return Executor(engine=engine, max_rows=100)


class TestExecution:
    def test_scalar_result(self, executor: Executor) -> None:
        assert float(executor.execute("SELECT SUM(amount) AS value FROM fct_orders").scalar()) == 35.75

    def test_rows_and_schema(self, executor: Executor) -> None:
        result = executor.execute("SELECT channel, SUM(amount) AS value FROM fct_orders GROUP BY 1")
        assert result.row_count == 2
        assert set(result.columns) == {"channel", "value"}
        assert "channel" in result.schema

    def test_empty_result_scalar_is_none(self, executor: Executor) -> None:
        assert executor.execute("SELECT amount AS value FROM fct_orders WHERE id = 999").scalar() is None

    def test_scalar_rejects_wrong_shape(self, executor: Executor) -> None:
        with pytest.raises(QueryError, match="expected a single value"):
            executor.execute("SELECT id, amount FROM fct_orders").scalar()

    def test_malformed_sql_raises_query_error_carrying_the_sql(self, executor: Executor) -> None:
        """The repair loop needs the failing SQL back, not just a message."""
        with pytest.raises(QueryError) as exc:
            executor.execute("SELECT nonexistent_column FROM fct_orders")
        assert exc.value.sql is not None

    def test_column_values(self, executor: Executor) -> None:
        values = executor.execute("SELECT channel FROM fct_orders ORDER BY id").column_values("channel")
        assert values == ["Paid", "Organic", "Paid"]

    def test_sample_rows_is_bounded(self, executor: Executor) -> None:
        assert len(executor.execute("SELECT * FROM fct_orders").sample_rows(2)) == 2


class TestCaching:
    def test_second_identical_query_is_served_from_cache(self, executor: Executor) -> None:
        sql = "SELECT SUM(amount) AS value FROM fct_orders"
        first = executor.execute(sql)
        second = executor.execute(sql)
        assert not first.from_cache
        assert second.from_cache
        assert first.scalar() == second.scalar()

    def test_freshness_bucket_busts_the_cache(self, executor: Executor) -> None:
        """A cached answer must expire when the data watermark moves, not on a TTL."""
        sql = "SELECT SUM(amount) AS value FROM fct_orders"
        executor.execute(sql, freshness="2026-08-01")
        assert not executor.execute(sql, freshness="2026-08-02").from_cache

    def test_cache_evicts_when_full(self) -> None:
        cache = ResultCache(max_entries=2)
        for i in range(3):
            cache.put(f"k{i}", None, [])  # type: ignore[arg-type]
        assert cache.get("k0") is None
        assert cache.get("k2") is not None

    def test_content_hash_is_stable_and_distinguishing(self, executor: Executor) -> None:
        a = executor.execute("SELECT amount AS value FROM fct_orders ORDER BY id")
        b = executor.execute("SELECT amount AS value FROM fct_orders ORDER BY id DESC")
        assert a.content_hash == a.content_hash
        assert a.content_hash != b.content_hash


class TestSafety:
    def test_hostile_sql_never_reaches_the_engine(self, executor: Executor) -> None:
        with pytest.raises(UnsafeSQL):
            executor.execute("DROP TABLE fct_orders")
        assert executor.execute("SELECT COUNT(*) AS value FROM fct_orders").scalar() == 3

    def test_internal_tables_are_hidden_from_discovery(self, executor: Executor) -> None:
        assert "fct_orders" in executor.allowlist()
        assert "_internal_audit" not in executor.allowlist()

    @pytest.mark.parametrize(
        "name,internal",
        [("_ground_truth", True), ("schema._private", True), ("fct_orders", False), ("a.b.c", False)],
    )
    def test_internal_table_detection(self, name: str, internal: bool) -> None:
        assert _is_internal(name) is internal

    def test_explain_reports_invalid_without_executing(self, executor: Executor) -> None:
        report = executor.explain("DELETE FROM fct_orders")
        assert report["valid"] is False
        assert report["reasons"]
        assert executor.execute("SELECT COUNT(*) AS value FROM fct_orders").scalar() == 3

    def test_explain_reports_lineage_for_valid_sql(self, executor: Executor) -> None:
        report = executor.explain("SELECT amount FROM fct_orders")
        assert report["valid"] is True
        assert report["tables"] == ["fct_orders"]

    def test_limit_is_applied_to_large_results(self, engine: DuckDBEngine) -> None:
        small = Executor(engine=engine, max_rows=2)
        assert small.execute("SELECT * FROM fct_orders").row_count == 2


class TestMetricPackLoading:
    def test_bundled_pack_loads(self) -> None:
        model = load_file(Path(__file__).resolve().parents[2] / "configs/metrics/ecommerce.yaml")
        assert model.metric("cac").kind == "ratio"
        assert model.entity("orders").dataset == "fct_orders"

    def test_directory_merges_packs(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            "entities:\n  - key: a\n    dataset: t_a\n"
            "metrics:\n  - key: m1\n    entity: a\n    agg: count\n    sql: '{{entity}}.id'\n"
        )
        (tmp_path / "b.yaml").write_text(
            "metrics:\n  - key: m2\n    entity: a\n    agg: count\n    sql: '{{entity}}.id'\n"
        )
        model = load_directory(tmp_path)
        assert {m.key for m in model.metrics} == {"m1", "m2"}

    def test_duplicate_metric_across_packs_is_an_error(self, tmp_path: Path) -> None:
        """Last-one-wins across files would make a metric's definition depend on
        filename ordering, which is an awful thing to debug."""
        body = (
            "entities:\n  - key: a\n    dataset: t_a\n"
            "metrics:\n  - key: dup\n    entity: a\n    agg: count\n    sql: '{{entity}}.id'\n"
        )
        (tmp_path / "a.yaml").write_text(body)
        (tmp_path / "b.yaml").write_text(body)
        with pytest.raises(SemanticLoadError, match="duplicate"):
            load_directory(tmp_path)

    def test_empty_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SemanticLoadError, match="no semantic definition files"):
            load_directory(tmp_path)


class TestFileLoading:
    """Parquet round-trip — the mechanism behind file uploads and the web demo."""

    def test_export_and_reload(self, engine: DuckDBEngine, tmp_path: Path) -> None:
        exported = engine.export_parquet(tmp_path)
        assert "fct_orders" in exported

        fresh = DuckDBEngine(":memory:", read_only=False)
        fresh.load_files({t: tmp_path / f for t, f in exported.items()})
        assert fresh.list_tables() == engine.list_tables()
        assert Executor(engine=fresh).execute(
            "SELECT COUNT(*) AS value FROM fct_orders"
        ).scalar() == 3

    def test_export_skips_internal_tables(self, engine: DuckDBEngine, tmp_path: Path) -> None:
        """_internal_audit must not be shipped to a browser or anywhere else."""
        assert not any(name.startswith("_") for name in engine.export_parquet(tmp_path))

    def test_values_survive_the_round_trip(self, engine: DuckDBEngine, tmp_path: Path) -> None:
        """A fixture that changes value in transit would silently invalidate evals."""
        before = Executor(engine=engine).execute(
            "SELECT SUM(amount) AS value FROM fct_orders"
        ).scalar()
        exported = engine.export_parquet(tmp_path)
        fresh = DuckDBEngine(":memory:", read_only=False)
        fresh.load_files({t: tmp_path / f for t, f in exported.items()})
        after = Executor(engine=fresh).execute("SELECT SUM(amount) AS value FROM fct_orders").scalar()
        assert float(before) == float(after)

    def test_csv_is_also_accepted(self, engine: DuckDBEngine, tmp_path: Path) -> None:
        csv = tmp_path / "uploaded.csv"
        csv.write_text("id,label\n1,alpha\n2,beta\n")
        fresh = DuckDBEngine(":memory:", read_only=False)
        fresh.load_files({"uploaded": csv})
        assert Executor(engine=fresh).execute(
            "SELECT COUNT(*) AS value FROM uploaded"
        ).scalar() == 2
