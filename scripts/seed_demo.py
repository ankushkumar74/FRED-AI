#!/usr/bin/env python3
"""Generate a synthetic e-commerce company in DuckDB, with planted ground truth.

Why synthetic rather than a public dataset: a root cause engine can only be
tested if you know the answer. This generator *injects* a specific failure —
Paid Search in EMEA hits a budget cap in the final week — so the eval suite can
ask "did it find the cause we planted?" rather than "did it produce prose?"
(BLUEPRINT 5.1, tests/fixtures).

Fully deterministic: same seed, same bytes, so evals are reproducible.

    python scripts/seed_demo.py [--out demo.duckdb]
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import duckdb

SEED = 20260802
END_DATE = date(2026, 8, 2)  # a Sunday, so the final week is complete
DAYS = 540

CHANNELS = ["Paid Search", "Paid Social", "Organic", "Email", "Affiliate"]
REGIONS = ["NA", "EMEA", "APAC"]
DEVICES = ["desktop", "mobile", "tablet"]
SEGMENTS = ["consumer", "pro", "enterprise"]
CATEGORIES = ["Apparel", "Footwear", "Accessories", "Home"]

CHANNEL_WEIGHT = {"Paid Search": 0.30, "Paid Social": 0.22, "Organic": 0.28, "Email": 0.12, "Affiliate": 0.08}
REGION_WEIGHT = {"NA": 0.52, "EMEA": 0.33, "APAC": 0.15}
CHANNEL_CPC = {"Paid Search": 1.45, "Paid Social": 0.85, "Organic": 0.0, "Email": 0.0, "Affiliate": 0.0}

# ---- the planted incident ----------------------------------------------
# Everything the eval suite asserts about root cause traces back to these four
# constants. Changing them changes the expected answer, so they live in one place.
INCIDENT_START = date(2026, 7, 27)
INCIDENT_END = END_DATE
INCIDENT_CHANNEL = "Paid Search"
INCIDENT_REGION = "EMEA"
INCIDENT_SEVERITY = 0.35  # spend and volume fall to 35% of normal


@dataclass
class GroundTruth:
    """Written to the database so tests read the planted answer, not a copy of it."""

    incident_start: date
    incident_end: date
    channel: str
    region: str
    severity: float
    description: str


def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _seasonality(day: date) -> float:
    """Weekly shape plus a mild annual cycle. Real data is never flat."""
    weekday_factor = [1.06, 1.02, 1.00, 1.01, 1.08, 0.86, 0.80][day.weekday()]
    month_factor = 1.0 + 0.18 * (1 if day.month in (11, 12) else 0) - 0.06 * (1 if day.month in (1, 2) else 0)
    return weekday_factor * month_factor


def _trend(index: int, total: int) -> float:
    """Gentle growth over the window, so trend detection has something to find."""
    return 0.82 + 0.36 * (index / max(total - 1, 1))


def generate(out_path: Path, days: int = DAYS) -> GroundTruth:
    rng = random.Random(SEED)
    start_date = END_DATE - timedelta(days=days - 1)

    orders: list[tuple] = []
    order_items: list[tuple] = []
    customers: dict[int, tuple] = {}
    spend_rows: list[tuple] = []

    customer_order_counts: dict[int, int] = {}
    next_customer_id = 1
    order_id = 1
    order_item_id = 1

    for index in range(days):
        day = start_date + timedelta(days=index)
        base_orders = 120 * _seasonality(day) * _trend(index, days)

        # --- marketing spend, by channel and region (shares dimensions with orders)
        for channel in CHANNELS:
            if CHANNEL_CPC[channel] == 0:
                continue
            for region in REGIONS:
                daily = 1800 * CHANNEL_WEIGHT[channel] * REGION_WEIGHT[region] * _seasonality(day)
                incident = (
                    INCIDENT_START <= day <= INCIDENT_END
                    and channel == INCIDENT_CHANNEL
                    and region == INCIDENT_REGION
                )
                if incident:
                    daily *= INCIDENT_SEVERITY
                spend = round(daily * rng.uniform(0.92, 1.08), 2)
                clicks = int(spend / CHANNEL_CPC[channel]) if CHANNEL_CPC[channel] else 0
                impressions = int(clicks * rng.uniform(22, 38))
                spend_rows.append(
                    (day, channel, region, f"{channel} - {region} - Always On", spend, impressions, clicks)
                )

        # --- orders
        n_orders = max(int(rng.gauss(base_orders, base_orders * 0.07)), 10)
        for _ in range(n_orders):
            channel = _weighted(rng, CHANNEL_WEIGHT)
            region = _weighted(rng, REGION_WEIGHT)

            # The planted incident suppresses order volume in exactly one slice.
            if (
                INCIDENT_START <= day <= INCIDENT_END
                and channel == INCIDENT_CHANNEL
                and region == INCIDENT_REGION
                and rng.random() > INCIDENT_SEVERITY
            ):
                continue

            returning = rng.random() < 0.46 and customer_order_counts
            if returning:
                customer_id = rng.choice(list(customer_order_counts.keys()))
            else:
                customer_id = next_customer_id
                next_customer_id += 1
                customers[customer_id] = (
                    customer_id,
                    day,
                    _weighted(rng, {"consumer": 0.70, "pro": 0.22, "enterprise": 0.08}),
                    region,
                )

            sequence = customer_order_counts.get(customer_id, 0) + 1
            customer_order_counts[customer_id] = sequence

            n_items = rng.choices([1, 2, 3, 4], weights=[0.52, 0.28, 0.14, 0.06], k=1)[0]
            item_rows = []
            order_total = 0.0
            for _ in range(n_items):
                category = rng.choice(CATEGORIES)
                quantity = rng.choices([1, 2, 3], weights=[0.78, 0.17, 0.05], k=1)[0]
                unit = round(rng.uniform(18, 140), 2)
                line_total = round(unit * quantity, 2)
                order_total += line_total
                item_rows.append((category, f"{category[:3].upper()}-{rng.randint(1000, 9999)}", quantity, line_total))

            order_total = round(order_total, 2)
            discount = round(order_total * rng.choice([0, 0, 0, 0.05, 0.10, 0.15]), 2)
            refund = round(order_total * rng.choice([0] * 18 + [0.5, 1.0]), 2)
            status = rng.choices(
                ["complete", "cancelled", "test"], weights=[0.962, 0.035, 0.003], k=1
            )[0]
            is_internal = rng.random() < 0.004

            orders.append(
                (
                    order_id,
                    customer_id,
                    day,
                    order_total,
                    discount,
                    refund,
                    status,
                    channel,
                    region,
                    _weighted(rng, {"desktop": 0.44, "mobile": 0.48, "tablet": 0.08}),
                    sequence,
                    is_internal,
                )
            )
            for category, sku, quantity, line_total in item_rows:
                order_items.append((order_item_id, order_id, sku, category, quantity, line_total))
                order_item_id += 1
            order_id += 1

    truth = GroundTruth(
        incident_start=INCIDENT_START,
        incident_end=INCIDENT_END,
        channel=INCIDENT_CHANNEL,
        region=INCIDENT_REGION,
        severity=INCIDENT_SEVERITY,
        description=(
            f"{INCIDENT_CHANNEL} in {INCIDENT_REGION} fell to {INCIDENT_SEVERITY:.0%} of normal "
            f"spend and order volume from {INCIDENT_START} (budget cap)"
        ),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    con = duckdb.connect(str(out_path))
    try:
        _write(con, orders, order_items, customers, spend_rows, truth)
    finally:
        con.close()
    return truth


def _write(con, orders, order_items, customers, spend_rows, truth: GroundTruth) -> None:
    con.execute(
        """
        CREATE TABLE fct_orders (
            order_id         BIGINT,
            customer_id      BIGINT,
            ordered_at       DATE,
            order_total      DECIMAL(18, 2),
            discount_amount  DECIMAL(18, 2),
            refund_amount    DECIMAL(18, 2),
            status           VARCHAR,
            channel          VARCHAR,
            region           VARCHAR,
            device           VARCHAR,
            order_sequence   INTEGER,
            is_internal      BOOLEAN
        )
        """
    )
    con.execute(
        """
        CREATE TABLE fct_order_items (
            order_item_id BIGINT, order_id BIGINT, sku VARCHAR,
            category VARCHAR, quantity INTEGER, line_total DECIMAL(18, 2)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE dim_customers (
            customer_id BIGINT, signup_at DATE, segment VARCHAR, country VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE fct_marketing_spend (
            spend_date DATE, channel VARCHAR, region VARCHAR, campaign VARCHAR,
            spend DECIMAL(18, 2), impressions BIGINT, clicks BIGINT
        )
        """
    )
    # Ground truth lives in the database so a test cannot drift from the generator.
    con.execute(
        """
        CREATE TABLE _ground_truth (
            incident_start DATE, incident_end DATE, channel VARCHAR,
            region VARCHAR, severity DOUBLE, description VARCHAR
        )
        """
    )

    # Columnar bulk load, not executemany. Row-at-a-time insertion of ~175k rows
    # takes over a minute; going through Arrow takes about a second, and a fixture
    # nobody wants to rebuild is a fixture that goes stale.
    _bulk_insert(con, "fct_orders", orders, [
        "order_id", "customer_id", "ordered_at", "order_total", "discount_amount",
        "refund_amount", "status", "channel", "region", "device", "order_sequence", "is_internal",
    ])
    _bulk_insert(con, "fct_order_items", order_items, [
        "order_item_id", "order_id", "sku", "category", "quantity", "line_total",
    ])
    _bulk_insert(con, "dim_customers", list(customers.values()), [
        "customer_id", "signup_at", "segment", "country",
    ])
    _bulk_insert(con, "fct_marketing_spend", spend_rows, [
        "spend_date", "channel", "region", "campaign", "spend", "impressions", "clicks",
    ])
    con.execute(
        "INSERT INTO _ground_truth VALUES (?,?,?,?,?,?)",
        [
            truth.incident_start,
            truth.incident_end,
            truth.channel,
            truth.region,
            truth.severity,
            truth.description,
        ],
    )


def _bulk_insert(con, table: str, rows: list[tuple], columns: list[str]) -> None:
    if not rows:
        return
    import pyarrow as pa

    arrow_table = pa.table({name: [row[i] for row in rows] for i, name in enumerate(columns)})
    con.register("_staging", arrow_table)
    con.execute(f"INSERT INTO {table} SELECT * FROM _staging")  # table name is a literal, not input
    con.unregister("_staging")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="demo.duckdb", help="output DuckDB file")
    parser.add_argument(
        "--days", type=int, default=DAYS, help="days of history (tests use a short window)"
    )
    args = parser.parse_args()

    out = Path(args.out)
    truth = generate(out, days=args.days)

    con = duckdb.connect(str(out), read_only=True)
    counts = {
        table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("fct_orders", "fct_order_items", "dim_customers", "fct_marketing_spend")
    }
    span = con.execute("SELECT MIN(ordered_at), MAX(ordered_at) FROM fct_orders").fetchone()
    con.close()

    print(f"wrote {out}")
    for table, count in counts.items():
        print(f"  {table:<22} {count:>8,} rows")
    print(f"  date range             {span[0]} .. {span[1]}")
    print(f"  planted ground truth   {truth.description}")


if __name__ == "__main__":
    main()
