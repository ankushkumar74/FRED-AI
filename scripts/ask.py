#!/usr/bin/env python3
"""Ask FRED a question from the terminal.

A thin shell over `services.ask.Ask` — the same object the browser demo calls, so
the two cannot drift. Everything here is formatting.

    python scripts/seed_demo.py --out demo.duckdb
    python scripts/ask.py "revenue by channel last month"
    python scripts/ask.py "what is our conversion rate"     # declines, on purpose
    python scripts/ask.py "revenue by product category"     # refuses, on purpose
    python scripts/ask.py "why did revenue drop last week" --trace

Every answer prints its provenance. That is not a debug flag — it is the trust
mechanism (blueprint 1.2, principle 2), and an analyst who cannot see the SQL
will not let this near their CEO.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.ask import Answer, Ask


def format_value(value: Any, fmt: str) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    if fmt == "currency":
        return f"${number:,.2f}"
    if fmt == "percent":
        return f"{number * 100:,.2f}%"
    return f"{number:,.2f}"


def _short(value: Any) -> str:
    text = str(value)
    return text[:10] if text[4:5] == "-" and len(text) > 10 else text


def render(answer: Answer, show_trace: bool) -> None:
    print(f"\n  Q: {answer.question}\n")

    if answer.status == "clarify":
        print(f"  {answer.message}\n")
        if answer.candidates:
            print("  closest candidates:")
            for c in answer.candidates[:3]:
                print(f"    {c['key']:<18} score={c['score']:.2f} {c['reasons']}")
        print()
        return

    if answer.status in ("refused", "error"):
        label = "I can't answer that correctly" if answer.status == "refused" else "Query failed"
        print(f"  {label}:\n    {answer.message}\n")
        return

    print(f"  {answer.scope}")
    print(f"  {'-' * len(answer.scope)}")

    if not answer.rows:
        print("  no data in that window\n")
        return

    if len(answer.rows) == 1 and len(answer.columns) == 1:
        print(f"  {format_value(answer.rows[0]['value'], answer.metric_format)}")
    else:
        labels = [c for c in answer.columns if c not in ("value", "period_type")]
        for row in answer.rows[:20]:
            # Dates arrive as ISO timestamps; keep the date and drop the 00:00:00.
            # Everything else prints in full — truncating turned "Paid Search"
            # into "Paid Searc".
            label = " / ".join(_short(row[c]) for c in labels)
            print(f"  {label:<28} {format_value(row['value'], answer.metric_format):>16}")
        if len(answer.rows) > 20:
            print(f"  ... {len(answer.rows) - 20} more rows")

    if answer.limitation:
        print(f"\n  Note: {answer.limitation}")

    print(
        f"\n  [{answer.duration_ms}ms"
        f"{', cached' if answer.from_cache else ''}"
        f" | {len(answer.rows)} rows | {', '.join(answer.tables)}"
        f" | metric {'certified' if answer.certified else 'uncertified'}]"
    )
    for note in answer.notes:
        print(f"  [note] {note}")
    for warning in answer.warnings:
        print(f"  [warn] {warning}")

    if show_trace and answer.sql:
        print("\n  --- SQL " + "-" * 50)
        for line in answer.sql.splitlines():
            print(f"  {line}")
    elif answer.sql:
        print("  (--trace to see the SQL)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+")
    parser.add_argument("--database", default="demo.duckdb")
    parser.add_argument("--metrics", default="configs/metrics/ecommerce.yaml")
    parser.add_argument("--trace", action="store_true", help="print the compiled SQL")
    args = parser.parse_args()

    database = Path(args.database)
    if not database.exists():
        print(f"fixture {database} not found. Run: python scripts/seed_demo.py --out {database}")
        return 2

    fred = Ask(metrics_path=Path(args.metrics), database=database)
    render(fred.answer(" ".join(args.question)), show_trace=args.trace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
