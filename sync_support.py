#!/usr/bin/env python3
"""Render the support tables in docs/support.md from support.json.

support.json is the source of truth for which databases Sustained runs
against and which Python versions it supports. This script writes the two
generated regions of the support page from it. Run it after editing
support.json; the pre-commit hook does this automatically and fails the
commit when the page is stale.

    python3 sync_support.py            # write the page
    python3 sync_support.py --check    # fail when the page is stale
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

LEVELS = ("runs", "builds")
SERVERS = ("container", "in-process", "account", "none")

DATABASES_BEGIN = "<!-- databases: generated from support.json -->"
DATABASES_END = "<!-- end databases -->"
PYTHON_BEGIN = "<!-- python: generated from support.json -->"
PYTHON_END = "<!-- end python -->"


def load(path: Path) -> Dict[str, Any]:
    """Read support.json and check that every row is well formed."""
    data: Dict[str, Any] = json.loads(path.read_text())
    for row in data["databases"]:
        if row["level"] not in LEVELS:
            raise ValueError(f"{row['name']}: level must be one of {LEVELS}")
        if row["server"] not in SERVERS:
            raise ValueError(f"{row['name']}: server must be one of {SERVERS}")
        if row["level"] == "builds" and row["covers"]:
            raise ValueError(f"{row['name']}: a builds row covers nothing")
        if row["level"] == "runs" and not row["covers"]:
            raise ValueError(f"{row['name']}: a runs row must cover something")
    return data


def _where(row: Dict[str, Any]) -> str:
    if row["server"] == "container":
        return f"Container ({row['image']})"
    if row["server"] == "in-process":
        return "In process"
    if row["server"] == "account":
        return "Your AWS account"
    return "Nothing to run"


def _covers(row: Dict[str, Any]) -> str:
    return ", ".join(row["covers"]) if row["covers"] else "SQL text only"


def render_databases(data: Dict[str, Any]) -> str:
    """The database table, one row per dialect, runs before builds."""
    header = [
        "| Database | Level | Where it runs | Covered | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows: List[str] = []
    for level in LEVELS:
        for row in data["databases"]:
            if row["level"] != level:
                continue
            rows.append(
                f"| {row['title']} | `{row['level']}` | {_where(row)} "
                f"| {_covers(row)} | {row['note']} |"
            )
    return "\n".join(header + rows)


def render_python(data: Dict[str, Any]) -> str:
    """One sentence naming the floor and every version the suite runs on."""
    versions = data["python"]["versions"]
    listed = ", ".join(versions[:-1]) + f", and {versions[-1]}"
    return (
        f"Sustained runs on CPython {data['python']['floor']} and later. "
        f"The test suite runs on {listed}."
    )


def _replace(text: str, begin: str, end: str, body: str) -> str:
    start = text.index(begin) + len(begin)
    stop = text.index(end)
    return text[:start] + "\n\n" + body + "\n\n" + text[stop:]


def render_page(page: str, data: Dict[str, Any]) -> str:
    page = _replace(page, DATABASES_BEGIN, DATABASES_END, render_databases(data))
    return _replace(page, PYTHON_BEGIN, PYTHON_END, render_python(data))


def main(argv: Tuple[str, ...]) -> int:
    root = Path(__file__).resolve().parent
    source = root / "support.json"
    target = root / "docs" / "support.md"

    for path in (source, target):
        if not path.exists():
            print(f"error: {path} not found.", file=sys.stderr)
            return 1

    rendered = render_page(target.read_text(), load(source))

    if target.read_text() == rendered:
        return 0
    if "--check" in argv:
        print(
            "docs/support.md is out of date. Run: python3 sync_support.py",
            file=sys.stderr,
        )
        return 1

    target.write_text(rendered)
    print(f"Wrote {target.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv[1:])))
