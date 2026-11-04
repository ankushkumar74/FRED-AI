#!/usr/bin/env python3
"""Assemble the browser demo into site_docs/demo/.

The demo runs the real package, not a reimplementation: this builds a wheel from
the current source and ships it alongside the fixture, so what the website runs
is exactly what `make test` covers. If the two could drift, the demo would stop
being evidence of anything.

    python scripts/build_demo.py          # expects demo.duckdb to exist
    python scripts/build_demo.py --seed   # generate the fixture first

Output is gitignored; CI rebuilds it on every deploy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUT = ROOT / "site_docs" / "demo"
METRICS = ROOT / "configs" / "metrics" / "ecommerce.yaml"


def build_wheel(dest: Path) -> str:
    """Build the package wheel into `dest` and return its filename."""
    staging = ROOT / "build" / "wheel"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # `pip wheel --no-deps .` rather than `build`, because pip is always present
    # on a runner and `build` is one more dependency to install for one command.
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(staging), str(ROOT)],
        check=True,
        stdout=subprocess.PIPE,
    )
    wheels = sorted(staging.glob("fred_ai-*.whl"))
    if not wheels:
        raise SystemExit("no wheel produced — check the [build-system] table in pyproject.toml")
    wheel = wheels[-1]
    shutil.copy2(wheel, dest / wheel.name)
    return wheel.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(ROOT / "demo.duckdb"))
    parser.add_argument("--seed", action="store_true", help="generate the fixture first")
    args = parser.parse_args()

    database = Path(args.database)
    if args.seed or not database.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "seed_demo.py"), "--out", str(database)],
            check=True,
            stdout=subprocess.PIPE,
        )

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for name in ("index.html", "fred.js", "bridge.py"):
        shutil.copy2(WEB / name, OUT / name)
    shutil.copy2(METRICS, OUT / "ecommerce.yaml")
    shutil.copy2(database, OUT / "demo.duckdb")

    wheel_name = build_wheel(OUT)
    (OUT / "manifest.json").write_text(
        json.dumps({"wheel": wheel_name, "fixture": "demo.duckdb"}, indent=2) + "\n"
    )

    total = sum(f.stat().st_size for f in OUT.iterdir()) / 1_000_000
    print(f"built demo into {OUT.relative_to(ROOT)} ({total:.1f} MB)")
    for f in sorted(OUT.iterdir()):
        print(f"  {f.stat().st_size / 1000:>8.0f} KB  {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
