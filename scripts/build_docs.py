#!/usr/bin/env python3
"""Split docs/BLUEPRINT.md into per-part pages for the MkDocs site.

BLUEPRINT.md stays the single source of truth. The site is generated from it at
build time so the two can never drift — editing the site directly is not a
supported workflow.

Usage:  python scripts/build_docs.py
Output: site_docs/ (gitignored, produced fresh by CI)
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "BLUEPRINT.md"
OUT = ROOT / "site_docs"


def slugify(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text.lower()).strip().replace(" ", "-")


def main() -> None:
    raw = SRC.read_text(encoding="utf-8")
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # Everything before "# Part 1" becomes the landing page.
    parts = re.split(r"^# Part (\d+) — (.+)$", raw, flags=re.MULTILINE)
    preamble, rest = parts[0], parts[1:]

    nav_lines: list[str] = []
    pages: list[tuple[str, str]] = []

    for i in range(0, len(rest), 3):
        number, title, body = rest[i], rest[i + 1], rest[i + 2]
        slug = f"part-{int(number):02d}-{slugify(title)}"
        pages.append((slug, f"# Part {number} — {title}\n{body}"))
        nav_lines.append(f"  - Part {number} — {title}: {slug}.md")

    for slug, content in pages:
        (OUT / f"{slug}.md").write_text(content, encoding="utf-8")

    # Landing page: strip the original TOC table, point it at the split pages.
    index = re.sub(r"## Table of contents.*?(?=^---$)", "", preamble, flags=re.DOTALL | re.MULTILINE)
    index += "\n## Contents\n\n"
    for slug, content in pages:
        title = content.split("\n", 1)[0].lstrip("# ")
        index += f"- [{title}]({slug}.md)\n"
    index += (
        "\n---\n\n"
        "The full document is also available as a "
        "[single Markdown file](https://github.com/ankushkumar74/FRED-AI/blob/main/docs/BLUEPRINT.md).\n"
    )
    (OUT / "index.md").write_text(index, encoding="utf-8")

    # Emit nav for mkdocs.yml consumption.
    nav = "nav:\n  - Overview: index.md\n" + "\n".join(nav_lines) + "\n"
    (ROOT / "nav.generated.yml").write_text(nav, encoding="utf-8")

    print(f"built {len(pages)} part pages + index into {OUT}")


if __name__ == "__main__":
    main()
