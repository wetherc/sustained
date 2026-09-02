"""
Static reading of migration SQL, for previews that touch no database.

`MigrationStatement` is a statement with the migration it came from,
which is what a guard reads. `destructive_statements()` finds the statements that remove data or
drop a constraint, so a preview can label them. `summarize()` reduces one migration to the count
and the labels the `plan` command prints.

The scan is textual: it reads the words in a statement and parses no
SQL. It knows string literals and comments only well enough to keep
them out of the scan, so a drop written inside a literal or a comment
is not labelled. The label informs the operator, and the rehearsal gate
in `migrate` reads the same list.
"""

from __future__ import annotations

import re
from typing import (
    TYPE_CHECKING,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from sustained.migrations import Migration, migration_sql

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler

# One pass over a statement finds string literals, quoted identifiers and
# comments. A comment inside a literal is part of the literal, so the
# literal alternatives come first and a '--' inside quotes survives.
_TOKEN_RE = re.compile(
    r"'(?:[^']|'')*'"  # string literal, '' is an escaped quote
    r'|"(?:[^"]|"")*"'  # quoted identifier
    r"|`[^`]*`"  # MySQL quoted identifier
    r"|--[^\n]*"  # line comment
    r"|/\*.*?\*/",  # block comment
    re.DOTALL,
)
_WHITESPACE_RE = re.compile(r"\s+")
# DROP DATABASE always takes the data with it. DROP SCHEMA needs CASCADE
# to do so, since a plain DROP SCHEMA refuses a schema that holds
# anything.
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bDROP\s+TYPE\b|\bTRUNCATE\b"
    r"|\bDROP\s+CONSTRAINT\b|\bDROP\s+CHECK\b|\bDROP\s+FOREIGN\s+KEY\b"
    r"|\bDELETE\s+FROM\b|\bDROP\s+(?:MATERIALIZED\s+)?VIEW\b"
    r"|\bDROP\s+DATABASE\b|\bDROP\s+SCHEMA\b[^;]*\bCASCADE\b",
    re.IGNORECASE,
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


class MigrationStatement(str):
    """
    One statement with the migration it came from.

    It is a `str`, so a guard reads it as the statement text and a guard
    written against `Sequence[str]` needs no change. `migration_id` names
    the migration the statement belongs to, and `transactional` says
    whether that migration runs inside a transaction. A rule about a
    setting that dies at a commit, such as `SET LOCAL`, reads the two to
    tell one migration from the next.

    `migration_id` is None for a statement that reached a guard with no
    migration around it. Statements that carry the same id in a row
    belong to one migration, so None statements next to each other read
    as one group.
    """

    migration_id: Optional[str]
    transactional: bool

    def __new__(
        cls,
        statement: str,
        migration_id: Optional[str] = None,
        transactional: bool = True,
    ) -> "MigrationStatement":
        instance = super().__new__(cls, statement)
        instance.migration_id = migration_id
        instance.transactional = transactional
        return instance


def statement_scope(statement: str) -> Tuple[Optional[str], bool]:
    """
    The migration a statement came from and whether that migration is
    transactional. A plain `str` carries neither, and reads as an
    unnamed statement inside a transaction.
    """
    if isinstance(statement, MigrationStatement):
        return statement.migration_id, statement.transactional
    return None, True


def _rewrite_tokens(statement: str, blank_literals: bool) -> str:
    """
    Removes the comments from a statement. When `blank_literals` is true,
    it also empties every string literal and quoted identifier, so words
    inside quotes cannot match a scan. A quote that never closes is not a
    token, so its text stays and reads as plain SQL.
    """

    def replace(match: "re.Match[str]") -> str:
        token = match.group(0)
        if token.startswith("--") or token.startswith("/*"):
            return ""
        if not blank_literals:
            return token
        return token[0] + token[-1]

    return _TOKEN_RE.sub(replace, statement)


def normalize_statement(statement: str) -> str:
    """
    One statement on one line: comments removed, whitespace collapsed,
    ends trimmed. This is the form a statement prints in, so string
    literals keep their text. A '--' inside a literal starts no comment.
    """
    return _WHITESPACE_RE.sub(" ", _rewrite_tokens(statement, False)).strip()


def scannable_statement(statement: str) -> str:
    """
    The form a textual scan reads: `normalize_statement()` with every
    string literal and quoted identifier emptied. A commented-out drop
    and a drop written inside quotes both match nothing. Print
    `normalize_statement()` instead; this form loses text.
    """
    return _WHITESPACE_RE.sub(" ", _rewrite_tokens(statement, True)).strip()


def destructive_statements(statements: Union[str, Sequence[str]]) -> List[str]:
    """
    Returns the statements that remove something the schema cannot give
    back: DROP TABLE, DROP COLUMN, DROP TYPE, DROP VIEW, DROP
    MATERIALIZED VIEW, DROP DATABASE, DROP SCHEMA ... CASCADE, TRUNCATE,
    DELETE FROM, a MySQL-style column drop that omits the COLUMN keyword
    (`ALTER TABLE t DROP col`), and constraint drops (DROP CONSTRAINT,
    DROP CHECK, DROP FOREIGN KEY). A dropped constraint removes no rows,
    but re-adding it needs the data to still satisfy it. A plain DROP
    SCHEMA refuses a schema that holds anything, so only the CASCADE form
    is labelled. Drops of indexes and keys are not labelled.

    Comments are removed and whitespace is collapsed, so each statement
    comes back on one line and a commented-out drop is not labelled. Both
    `--` and `/* */` comments are handled. The scan reads no text inside
    quotes, so a statement that names a drop in a string literal is not
    labelled.
    """
    if isinstance(statements, str):
        statements = [statements]
    found = []
    for statement in statements:
        scanned = scannable_statement(statement)
        if _DESTRUCTIVE_RE.search(scanned) or _ALTER_DROP_RE.search(scanned):
            found.append(normalize_statement(statement))
    return found


class PendingSummary(NamedTuple):
    """
    What a preview says about one migration that has not run yet.

    `sql` holds the statements the up step would run, and is None for a
    callable step, which has no SQL to render or scan. Each one is a
    MigrationStatement, so a guard reading them can tell which migration
    they came from.
    """

    id: str
    state: str
    repeatable: bool
    sql: Optional[List[str]]
    destructive: List[str]


def summarize(
    migration: Migration, state: str, compiler: Optional["Compiler"] = None
) -> PendingSummary:
    """
    Reduces one migration to its id, its state ('pending' or, for a
    repeatable whose contents changed, 'changed'), the statements its up
    step would run, and the ones that remove data. Ddl steps render for
    the given compiler's dialect, or ANSI when none is given.
    """
    if callable(migration.up):
        return PendingSummary(migration.id, state, migration.repeatable, None, [])
    statements: List[str] = [
        MigrationStatement(sql, migration.id, migration.transactional)
        for sql in migration_sql(migration, "up", compiler)
    ]
    return PendingSummary(
        migration.id,
        state,
        migration.repeatable,
        statements,
        destructive_statements(statements),
    )
