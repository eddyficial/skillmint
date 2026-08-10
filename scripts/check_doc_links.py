"""Verify every local (non-http) Markdown link at the repo root resolves to a real file.

Run directly: python scripts/check_doc_links.py
"""
from __future__ import annotations

import pathlib
import re
import sys

_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    md_files = sorted(repo_root.glob("*.md"))
    failures: list[str] = []

    for md in md_files:
        text = md.read_text(encoding="utf-8")
        for match in _LINK_RE.finditer(text):
            target = match.group(1).split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                failures.append(f"{md.name}: broken link -> {target}")

    if failures:
        print("\n".join(failures))
        return 1
    print(f"Checked {len(md_files)} markdown file(s): {[m.name for m in md_files]}, no broken local links.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
