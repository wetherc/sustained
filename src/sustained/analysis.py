"""
Static reading of migration SQL, for previews that touch no database.

`destructive_statements()` finds the statements that remove data, so a
preview can label them. `summarize()` reduces one migration to the count
and the labels the `plan` command prints.

The scan is textual. A drop named inside a string literal is labelled
too, since nothing here parses SQL. The label informs the operator; it
never blocks a run.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Sequence, Union

from sustained.migrations import Migration, migration_sql

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bTRUNCATE\b", re.IGNORECASE
)
# MySQL lets a column drop omit the COLUMN keyword. This matches
# `ALTER TABLE <name> DROP <identifier>` while it skips drops of other
# schema objects, such as a constraint, an index, or a key.
_ALTER_DROP_RE = re.compile(
    r"\bALTER\s+TABLE\s+\S+\s+DROP\s+"
    r"(?!CONSTRAINT\b|INDEX\b|KEY\b|FOREIGN\b|PRIMARY\b|CHECK\b|PARTITION\b)"
    r"[A-Za-z_`\"\[]",
    re.IGNORECASE,
)


def destructive_statements(statements: Union[str, Sequence[str]]) -> List[str]:
    """
    Returns the statements that remove data: DROP TABLE, DROP COLUMN,
    TRUNCATE, and a MySQL-style column drop that omits the COLUMN
    keyword (`ALTER TABLE t DROP col`). Drops of constraints, indexes,
    and keys are not labelled. Comments are removed and whitespace is
    collapsed, so each
    statement comes back on one line and a commented-out drop is not
    labelled. Both `--` and `/* */` comments are handled.
    """
    if isinstance(statements, str):
        statements = [statements]
    found = []
    for statement in statements:
        stripped = _WHITESPACE_RE.sub(" ", _COMMENT_RE.sub("", statement)).strip()
        if _DESTRUCTIVE_RE.search(stripped) or _ALTER_DROP_RE.search(stripped):
            found.append(stripped)
    return found


class PendingSummary(NamedTuple):
    """
    What a preview says about one migration that has not run yet.

    `sql` holds the statements the up step would run, and is None for a
    callable step, which has no SQL to render or scan.
    """

    id: str
    state: str
    repeatable: bool
    sql: Optional[List[str]]
    destructive: List[str]


def summarize(migration: Migration, state: str) -> PendingSummary:
    """
    Reduces one migration to its id, its state ('pending' or, for a
    repeatable whose contents changed, 'changed'), the statements its up
    step would run, and the ones that remove data.
    """
    if callable(migration.up):
        return PendingSummary(migration.id, state, migration.repeatable, None, [])
    statements = migration_sql(migration, "up")
    return PendingSummary(
        migration.id,
        state,
        migration.repeatable,
        statements,
        destructive_statements(statements),
    )
