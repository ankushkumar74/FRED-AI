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

_FRED: Ask | None = None
_BOOT_MS: int = 0


def boot(metrics_path: str = "/fred/ecommerce.yaml", database: str = "/fred/demo.duckdb") -> str:
    """Load the metric pack and open the fixture. Returns a JSON status blob."""
    global _FRED, _BOOT_MS
    started = time.perf_counter()
    try:
        _FRED = Ask(metrics_path=Path(metrics_path), database=Path(database))
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
