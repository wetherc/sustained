"""
Static reading of migration SQL, for previews that touch no database.

`destructive_statements()` finds the statements that remove data, so a
preview can label them. `summarize()` reduces one migration to the count
and the labels the `plan` command prints.

The scan is textual. A drop named inside a string literal is labelled
too. The label informs the operator; it never blocks a run.
"""

from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Sequence, Union

from sustained.migrations import Migration, migration_sql

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_WHITESPACE_RE = re.compile(r"\s+")
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bTRUNCATE\b", re.IGNORECASE
)


def destructive_statements(statements: Union[str, Sequence[str]]) -> List[str]:
    """
    Returns the statements that remove data: DROP TABLE, DROP COLUMN, and
    TRUNCATE. Each is returned on one line, whitespace collapsed, so a
    multi-line statement stays readable in a list. Line comments are
    ignored, so a commented-out drop is not labelled.
    """
    if isinstance(statements, str):
        statements = [statements]
    found = []
    for statement in statements:
        if _DESTRUCTIVE_RE.search(_LINE_COMMENT_RE.sub("", statement)):
            found.append(_WHITESPACE_RE.sub(" ", statement).strip())
    return found


class PendingSummary(NamedTuple):
    """
    What a preview says about one migration that has not run yet.

    `statements` is None for a callable step, which has no SQL to count
    or scan.
    """

    id: str
    state: str
    repeatable: bool
    statements: Optional[int]
    destructive: List[str]


def summarize(migration: Migration, state: str) -> PendingSummary:
    """
    Reduces one migration to its id, its state ('pending' or, for a
    repeatable whose contents changed, 'changed'), its statement count,
    and the statements that remove data.
    """
    if callable(migration.up):
        return PendingSummary(migration.id, state, migration.repeatable, None, [])
    statements = migration_sql(migration, "up")
    return PendingSummary(
        migration.id,
        state,
        migration.repeatable,
        len(statements),
        destructive_statements(statements),
    )
