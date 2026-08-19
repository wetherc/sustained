#!/usr/bin/env python3
"""Render CHANGELOG.md as the docs site's changelog page.

The repository's CHANGELOG.md is the source of truth. This script copies it
into docs/changelog.md with the Jekyll front matter and preamble the site
needs. Run it after editing the changelog; the pre-commit hook does this
automatically and fails the commit when the page is stale.
"""

import sys
from pathlib import Path

PREAMBLE = """---
layout: default
title: Changelog
---

Every released version of Sustained, newest first. The same text lives in `CHANGELOG.md` in the repository; this page is generated from it.

Version numbers follow semantic versioning. A major version marks a change that can break working code. A minor version adds new features. A patch version fixes a defect without changing public API signatures or introducing new functionality.
"""


def render(changelog_text: str) -> str:
    """Strip the changelog's own H1 and wrap the rest in front matter."""
    lines = changelog_text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    return f"{PREAMBLE}\n{body}\n"


def main() -> int:
    project_root = Path(__file__).resolve().parent
    source = project_root / "CHANGELOG.md"
    target = project_root / "docs" / "changelog.md"

    if not source.exists():
        print(f"Error: {source} not found.", file=sys.stderr)
        return 1

    rendered = render(source.read_text())
    check_only = "--check" in sys.argv

    if target.exists() and target.read_text() == rendered:
        return 0

    if check_only:
        print(
            "docs/changelog.md is out of date. Run: python3 sync_changelog.py",
            file=sys.stderr,
        )
        return 1

    target.write_text(rendered)
    print(f"Wrote {target.relative_to(project_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
