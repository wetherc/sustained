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
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

SERVERS = ("container", "in-process", "account", "none")
SERVICE_LINE = re.compile(r"^ {2}[A-Za-z0-9_-]+:\s*$")

DATABASES_BEGIN = "<!-- databases: generated from support.json -->"
DATABASES_END = "<!-- end databases -->"
PYTHON_BEGIN = "<!-- python: generated from support.json -->"
PYTHON_END = "<!-- end python -->"


def compose_services(path: Path) -> List[str]:
    """The service names in the compose file, read without a YAML parser."""
    services: List[str] = []
    inside = False
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.startswith("services:")
            continue
        if inside and SERVICE_LINE.match(line):
            services.append(line.strip().rstrip(":"))
    return services


def load(path: Path) -> Dict[str, Any]:
    """
    Read support.json and check that every row is well formed. A row with a
    server is a claim that the suite runs against it, so the row has to name
    a test module, and a container row has to name a compose service. Without
    these checks the table could claim coverage that does not exist.
    """
    root = path.parent
    data: Dict[str, Any] = json.loads(path.read_text())
    services = compose_services(root / "docker" / "compose.yaml")
    for row in data["databases"]:
        name = row["name"]
        if row["server"] not in SERVERS:
            raise ValueError(f"{name}: server must be one of {SERVERS}")
        if row["server"] == "none":
            if row["covers"]:
                raise ValueError(f"{name}: a row with no server covers nothing")
            continue
        if not row["covers"]:
            raise ValueError(f"{name}: a row with a server must cover something")
        if not row.get("floor"):
            raise ValueError(
                f"{name}: a row with a server must name a floor, the oldest "
                "server release every statement Sustained generates is valid on"
            )
        module = root / "tests" / "integration" / f"test_{name}.py"
        if not module.exists():
            raise ValueError(
                f"{name}: a row with a server needs "
                f"{module.relative_to(root)}. Add the module, or set the "
                "row's server to none."
            )
        if row["server"] == "container" and row.get("service") not in services:
            raise ValueError(
                f"{name}: no '{row.get('service')}' service in "
                "docker/compose.yaml. Add the service, or set the row's "
                "server to none."
            )
        latest = row.get("latest")
        if latest is None:
            continue
        if row["server"] != "container":
            raise ValueError(f"{name}: only a container row can carry latest")
        for key in ("image", "version", "service", "dsn_env", "dsn"):
            if not latest.get(key):
                raise ValueError(f"{name}: latest must name {key}")
        if latest["service"] not in services:
            raise ValueError(
                f"{name}: no '{latest['service']}' service in "
                "docker/compose.yaml. Add the service, or drop the row's "
                "latest block."
            )
        if latest["dsn_env"] == row["dsn_env"]:
            raise ValueError(
                f"{name}: latest needs its own connection variable, so "
                "either server can be pointed elsewhere on its own"
            )
    return data


def _where(row: Dict[str, Any]) -> str:
    if row["server"] == "container":
        latest = row.get("latest")
        if latest:
            return f"Container ({row['image']}, {latest['image']})"
        return f"Container ({row['image']})"
    if row["server"] == "in-process":
        return "In process"
    if row["server"] == "account":
        return "Your AWS account"
    return "Nothing to run"


def _covers(row: Dict[str, Any]) -> str:
    return ", ".join(row["covers"]) if row["covers"] else "SQL text only"


def _versions(row: Dict[str, Any]) -> str:
    """The claimed range and the one version the suite runs."""
    floor = row.get("floor")
    if not floor:
        return "Any"
    tested = row.get("version")
    latest = row.get("latest")
    if tested and latest:
        return f"{floor} and later; suite runs {tested} and {latest['version']}"
    if tested:
        return f"{floor} and later; suite runs {tested}"
    return f"{floor} and later"


def render_databases(data: Dict[str, Any]) -> str:
    """The database table, one row per dialect, in support.json order."""
    header = [
        "| Database | Versions | Where it runs | Covered | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    rows: List[str] = []
    for row in data["databases"]:
        rows.append(
            f"| {row['title']} | {_versions(row)} "
            f"| {_where(row)} | {_covers(row)} | {row['note']} |"
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
