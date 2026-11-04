"""Browser-side entry point, executed inside Pyodide.

Thin on purpose. It loads the same wheel the CLI imports and calls the same
`services.ask.Ask`, so what the website does is what the test suite covers. If
this file grew a second implementation of the pipeline, the demo would stop
being evidence that the pipeline works.
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path

from services.ask import Ask
from services.query.executor import DuckDBEngine

_FRED: Ask | None = None
_BOOT_MS: int = 0


def boot(metrics_path: str = "/fred/ecommerce.yaml", data_dir: str = "/fred") -> str:
    """Load the metric pack, build an in-memory DuckDB from Parquet, and report.

    Parquet rather than a shipped .duckdb file: the database format is tied to a
    storage version, and the version CI writes with is newer than the one
    Pyodide's WASM build can read. Parquet does not move underneath us.
    """
    global _FRED, _BOOT_MS
    started = time.perf_counter()
    try:
        engine = DuckDBEngine(":memory:", read_only=False)
        tables = {f.stem: str(f) for f in sorted(Path(data_dir).glob("*.parquet"))}
        if not tables:
            raise RuntimeError(f"no parquet files found in {data_dir}")
        engine.load_files(tables)
        _FRED = Ask(metrics_path=Path(metrics_path), engine=engine)
        _BOOT_MS = int((time.perf_counter() - started) * 1000)
        return json.dumps(
            {
                "ok": True,
                "boot_ms": _BOOT_MS,
                "metrics": len(_FRED.model.metrics),
                "entities": len(_FRED.model.entities),
                "catalogue": _FRED.catalogue(),
                "tables": sorted(_FRED.executor.allowlist()),
            }
        )
    except Exception as exc:  # surfaced in the UI, not swallowed
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                           "traceback": traceback.format_exc()})


def ask(question: str) -> str:
    """Answer one question. Always returns JSON, never raises into JS."""
    if _FRED is None:
        return json.dumps({"status": "error", "message": "engine not booted"})
    try:
        return json.dumps(_FRED.answer(question).to_dict())
    except Exception as exc:
        return json.dumps(
            {
                "question": question,
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
