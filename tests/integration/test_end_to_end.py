"""End-to-end: question -> MetricRef -> SQL -> warehouse -> values.

Runs against the generated DuckDB fixture, so it needs no external services and
works offline in CI. Two things are verified that unit tests cannot:

1. The compiled SQL actually executes on a real engine and returns sane values.
2. The planted incident is *statistically separable* from ordinary noise — the
   fixture is only useful as root-cause ground truth if the planted slice is an
   outlier and the other slices are not. A test for the test data.
"""

from __future__ import annotations

import statistics
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.query.executor import DuckDBEngine, Executor  # noqa: E402
from services.semantic.compiler import Compiler  # noqa: E402
from services.semantic.loader import load_file  # noqa: E402
from services.semantic.models import (  # noqa: E402
    Comparison,
    Filter,
    MetricRef,
    TimeGrain,
    TimeRange,
)
from services.semantic.resolver import MetricResolver  # noqa: E402

pytest.importorskip("duckdb")

METRICS = ROOT / "configs" / "metrics" / "ecommerce.yaml"


def _robust_z(value: float, baseline: list[float]) -> float:
    """Median/MAD z-score — the same statistic the evidence engine will use.

    Mean and standard deviation are destroyed by a single outlier, which is
    self-defeating in a function whose whole job is finding outliers. The 0.6745
    factor rescales MAD to be comparable to a standard deviation for normal data.
    """
    median = statistics.median(baseline)
    mad = statistics.median([abs(x - median) for x in baseline])
    if mad == 0:
        spread = statistics.pstdev(baseline)
        return (value - median) / spread if spread else 0.0
    return 0.6745 * (value - median) / mad


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate a fresh fixture. Deterministic, and ~3s thanks to the Arrow bulk load."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import generate  # type: ignore[import-not-found]

    # Full history on purpose. A shorter window leaves too few weeks per slice for
    # a stable baseline, and low-volume slices then produce |z| > 6 from pure noise.
    path = tmp_path_factory.mktemp("fixture") / "demo.duckdb"
    generate(path)
    return path


@pytest.fixture(scope="module")
def stack(fixture_db: Path):
    model = load_file(METRICS)
    return (
        MetricResolver(model),
        Compiler(model, dialect="duckdb"),
        Executor(engine=DuckDBEngine(fixture_db), max_rows=100_000),
    )


class TestEndToEnd:
    def test_question_to_number(self, stack) -> None:
        resolver, compiler, executor = stack

        resolution = resolver.resolve("what was revenue last month")
        assert resolution.metric is not None
        assert resolution.metric.key == "gross_revenue"

        compiled = compiler.compile(
            MetricRef(
                metric=resolution.metric.key,
                time_range=TimeRange(start=date(2026, 7, 1), end=date(2026, 7, 31)),
            )
        )
        result = executor.execute(compiled.sql)
        assert result.scalar() > 0
        assert result.tables_read == ["fct_orders"]

    def test_cross_entity_ratio_executes(self, stack) -> None:
        """CAC joins two tables with different grains; it either works or it lies."""
        _, compiler, executor = stack
        compiled = compiler.compile(
            MetricRef(
                metric="cac",
                dimensions=["channel"],
                grain=TimeGrain.WEEK,
                time_range=TimeRange(start=date(2026, 6, 1), end=date(2026, 8, 2)),
            )
        )
        result = executor.execute(compiled.sql)
        assert result.row_count > 0
        values = [v for v in result.column_values("value") if v is not None]
        assert values and all(v > 0 for v in values)

    def test_cac_is_null_not_zero_where_spend_is_untracked(self, stack) -> None:
        """Organic has customers and no spend. CAC is undefined there, not zero.

        Reporting 0 would say acquisition is free; reporting NULL says we cannot
        compute it. The second is true.
        """
        _, compiler, executor = stack
        compiled = compiler.compile(
            MetricRef(
                metric="cac",
                dimensions=["channel"],
                time_range=TimeRange(start=date(2026, 7, 1), end=date(2026, 7, 31)),
            )
        )
        rows = executor.execute(compiled.sql).rows()
        organic = [r for r in rows if r.get("channel") == "Organic"]
        assert organic and organic[0]["value"] is None

    def test_filters_change_the_answer(self, stack) -> None:
        _, compiler, executor = stack
        window = TimeRange(start=date(2026, 7, 1), end=date(2026, 7, 31))
        total = executor.execute(
            compiler.compile(MetricRef(metric="gross_revenue", time_range=window)).sql
        ).scalar()
        paid = executor.execute(
            compiler.compile(
                MetricRef(
                    metric="gross_revenue",
                    time_range=window,
                    filters=[Filter(dimension="channel", operator="in", values=["Paid Search"])],
                )
            ).sql
        ).scalar()
        assert 0 < paid < total

    def test_dimension_split_sums_to_total(self, stack) -> None:
        """A breakdown that does not reconcile to the total is a decomposition bug."""
        _, compiler, executor = stack
        window = TimeRange(start=date(2026, 7, 1), end=date(2026, 7, 31))
        total = float(
            executor.execute(
                compiler.compile(MetricRef(metric="gross_revenue", time_range=window)).sql
            ).scalar()
        )
        by_region = executor.execute(
            compiler.compile(
                MetricRef(metric="gross_revenue", dimensions=["region"], time_range=window)
            ).sql
        )
        parts = sum(float(v) for v in by_region.column_values("value"))
        assert abs(parts - total) / total < 1e-9

    def test_period_comparison_returns_both_windows(self, stack) -> None:
        _, compiler, executor = stack
        compiled = compiler.compile(
            MetricRef(
                metric="gross_revenue",
                time_range=TimeRange(start=date(2026, 7, 27), end=date(2026, 8, 2)),
                comparison=Comparison.PRIOR_PERIOD,
            )
        )
        rows = executor.execute(compiled.sql).rows()
        labels = {r["period_type"] for r in rows}
        assert labels == {"current", "comparison"}


class TestFixtureGroundTruth:
    """The planted incident must be findable, and nothing else should look like it."""

    def test_planted_slice_is_a_statistical_outlier(self, stack, fixture_db: Path) -> None:
        import duckdb

        con = duckdb.connect(str(fixture_db), read_only=True)
        truth = con.execute(
            "SELECT channel, region, incident_start FROM _ground_truth"
        ).fetchone()
        # The window starts on a Monday on purpose. Starting mid-week leaves a
        # partial first bucket, which produces a ~700% week-over-week jump and
        # inflates the spread enough to hide a real 70% drop — the exact partial
        # period artifact the data quality gate exists to catch (BLUEPRINT 9.2
        # stage 12). An earlier version of this test had that bug.
        rows = con.execute(
            """
            SELECT date_trunc('week', ordered_at) AS w, channel, region, SUM(order_total) AS rev
            FROM fct_orders
            WHERE status NOT IN ('cancelled', 'test') AND is_internal = FALSE
              AND ordered_at >= DATE '2026-03-02'
            GROUP BY 1, 2, 3 ORDER BY 1
            """
        ).fetchall()
        con.close()

        series: dict[tuple[str, str], list[float]] = {}
        for _, channel, region, revenue in rows:
            series.setdefault((channel, region), []).append(float(revenue))

        # Volume floor. In a thin slice the MAD of weekly changes collapses and the
        # z-score explodes on noise alone — one slice averaging ~20 orders/week hit
        # |z| = 6.7 with nothing wrong with it. Pruning below a volume floor is what
        # BLUEPRINT 12.3 prescribes for the real decomposition search, for this reason.
        volume_floor = statistics.median([statistics.fmean(v) for v in series.values()]) * 0.25

        z_scores: dict[tuple[str, str], float] = {}
        for slice_key, revenues in series.items():
            if statistics.fmean(revenues) < volume_floor or len(revenues) < 12:
                continue
            changes = [revenues[i] / revenues[i - 1] - 1 for i in range(1, len(revenues))]
            z_scores[slice_key] = _robust_z(changes[-1], changes[:-1])

        planted = (truth[0], truth[1])
        assert planted in z_scores, "planted slice was pruned or produced no data"
        assert z_scores[planted] < -3, f"planted slice is not an outlier: z={z_scores[planted]:.1f}"

        # The property that actually matters: the planted cause must be clearly more
        # extreme than anything noise produces. Without this margin a root cause
        # engine scored against this fixture would be guessing.
        worst_noise = max(abs(z) for key, z in z_scores.items() if key != planted)
        assert abs(z_scores[planted]) > 1.8 * worst_noise, (
            f"planted z={z_scores[planted]:.1f} vs worst noise |z|={worst_noise:.1f}: "
            "not separable enough to serve as root-cause ground truth"
        )

    def test_ground_truth_table_is_not_queryable(self, stack) -> None:
        """The answer key must be invisible to the system under test."""
        _, _, executor = stack
        assert not any(t.startswith("_") for t in executor.allowlist())
