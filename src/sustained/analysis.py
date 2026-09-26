"""
Static reading of migration SQL, for previews that touch no database.

`MigrationStatement` is a statement with the migration it came from,
which is what a guard reads. `destructive_statements()` finds the statements that remove data or
drop a constraint, so a preview can label them. `summarize()` reduces one migration to the count
and the labels the `plan` command prints.

The scan is textual: it reads the words in a statement and parses no
SQL. It knows string literals, comments, and Postgres dollar-quoted
bodies only well enough to keep them out of the scan, so a drop written
inside a literal, a comment, or a `$$` function body is not labelled. The label informs the operator, and the rehearsal gate
in `migrate` reads the same list.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from sustained.impact.model import INTENT_KINDS, Intent
from sustained.impact.tokens import BACKSLASH_TOKEN_RE, TOKEN_RE
from sustained.migrations import Migration, migration_sql

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler

# One pass over a statement finds string literals, quoted identifiers,
# comments, and Postgres dollar-quoted bodies. The patterns live with the
# impact tokenizer, so the scan and the recognizer read literals alike.
# A statement with a backslash is also scanned with the MySQL reading, in
# which a backslash escapes the next character of a literal.
_TOKEN_RE = TOKEN_RE
_BACKSLASH_TOKEN_RE = BACKSLASH_TOKEN_RE
_WHITESPACE_RE = re.compile(r"\s+")
# A statement that runs a dollar-quoted body at once, and the tag that
# opens such a body.
_DO_RE = re.compile(r"\s*DO\b", re.IGNORECASE)
_DOLLAR_TAG_RE = re.compile(r"\$\w*\$")
# DROP DATABASE always takes the data with it. DROP SCHEMA needs CASCADE
# to do so, since a plain DROP SCHEMA refuses a schema that holds
# anything. A DELETE at the start of a statement, after a CTE, or in a
# MERGE branch removes rows without the FROM keyword on MSSQL and MySQL
# (`DELETE t WHERE ...`, `DELETE t1 FROM t1 JOIN ...`).
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+TABLE\b|\bDROP\s+COLUMN\b|\bDROP\s+TYPE\b|\bTRUNCATE\b"
    r"|\bDROP\s+CONSTRAINT\b|\bDROP\s+CHECK\b|\bDROP\s+FOREIGN\s+KEY\b"
    r"|\bDELETE\s+FROM\b|^DELETE\b|\)\s*DELETE\b|\bTHEN\s+DELETE\b"
    r"|\bDROP\s+(?:MATERIALIZED\s+)?VIEW\b"
    r"|\bDROP\s+DATABASE\b|\bDROP\s+SCHEMA\b[^;]*\bCASCADE\b",
    re.IGNORECASE,
)
# MySQL lets a column drop omit the COLUMN keyword. This matches
# `ALTER TABLE <name> DROP <identifier>` while it skips drops of other
# schema objects, such as a constraint, an index, or a key. The table
# name may follow IF EXISTS or ONLY, and the drop may be any action in a
# comma-separated list (`ALTER TABLE t ADD x int, DROP y`).
_ALTER_DROP_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?\S+\s+(?:[^;]*?,\s*)?"
    r"DROP\s+"
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

    `destructive` marks a statement that removes data although its text
    names no drop, such as a column type change that narrows the type.
    The diff against the models sets it, because only the diff knows the
    type the column has today. `destructive_statements()` labels such a
    statement whatever its text says. When `destructive` is not given, a
    statement wrapped again keeps the mark of the statement it wraps.

    `intent` is what the statement is meant to do, as the code that
    generated it knows it: the diff and `DdlStep` rendering set it, and
    hand-written SQL has none. The impact analysis reads it before the
    text. Like `destructive`, a statement wrapped again keeps the intent
    of the statement it wraps when none is given. Neither attribute
    takes part in equality or in a migration's checksum, which reads the
    statement text alone.
    """

    migration_id: Optional[str]
    transactional: bool
    destructive: bool
    intent: Optional[Intent]

    def __new__(
        cls,
        statement: str,
        migration_id: Optional[str] = None,
        transactional: bool = True,
        destructive: Optional[bool] = None,
        intent: Optional[Intent] = None,
    ) -> "MigrationStatement":
        instance = super().__new__(cls, statement)
        instance.migration_id = migration_id
        instance.transactional = transactional
        if destructive is None:
            destructive = (
                isinstance(statement, MigrationStatement) and statement.destructive
            )
        instance.destructive = destructive
        if intent is None and isinstance(statement, MigrationStatement):
            intent = statement.intent
        instance.intent = intent
        return instance


def with_intent(
    statement: str,
    kind: str,
    table: Optional[str],
    column: Optional[str] = None,
    **details: object,
) -> MigrationStatement:
    """
    The statement with an `Intent` attached, keeping whatever else a
    MigrationStatement it wraps carries, such as the destructive mark.
    `kind` must be one of `sustained.impact.model.INTENT_KINDS`.
    """
    if kind not in INTENT_KINDS:
        raise ValueError(f"Unknown intent kind: {kind!r}.")
    intent = Intent(kind, table, column, MappingProxyType(details))
    if isinstance(statement, MigrationStatement):
        return MigrationStatement(
            statement,
            statement.migration_id,
            statement.transactional,
            statement.destructive,
            intent,
        )
    return MigrationStatement(statement, intent=intent)


def statement_scope(statement: str) -> Tuple[Optional[str], bool]:
    """
    The migration a statement came from and whether that migration is
    transactional. A plain `str` carries neither, and reads as an
    unnamed statement inside a transaction.
    """
    if isinstance(statement, MigrationStatement):
        return statement.migration_id, statement.transactional
    return None, True


def _rewrite_tokens(
    statement: str, blank_literals: bool, tokens: "re.Pattern[str]" = _TOKEN_RE
) -> str:
    """
    Removes the comments from a statement. When `blank_literals` is true,
    it also empties every string literal, quoted identifier, and
    dollar-quoted body, so words inside quotes cannot match a scan. A
    quote that never closes is not a token, so its text stays and reads
    as plain SQL.

    A `DO` block is the exception: Postgres runs its body as soon as the
    statement runs, so the body is scanned as SQL of its own. A function
    body runs only when something calls the function, and stays blank.
    """
    executes_body = blank_literals and _DO_RE.match(
        _rewrite_tokens(statement, False, tokens)
    )

    def replace(match: "re.Match[str]") -> str:
        token = match.group(0)
        if token.startswith("--") or token.startswith("/*"):
            return ""
        if not blank_literals:
            return token
        if token.startswith("$"):
            if executes_body:
                tag = _DOLLAR_TAG_RE.match(token)
                assert tag is not None
                body = token[tag.end() : len(token) - tag.end()]
                return f" {_rewrite_tokens(body, True, tokens)} "
            return "$$"
        return token[0] + token[-1]

    return tokens.sub(replace, statement)


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


def scannable_forms(statement: str) -> Tuple[str, ...]:
    """
    Every form a scan for a drop reads: `scannable_statement()`, and for
    a statement with a backslash also the form in which a backslash
    escapes the next character of a literal, as MySQL reads it. A drop
    found in either form counts, so a literal that one reading ends early
    cannot hide a drop from the scan.
    """
    forms = (scannable_statement(statement),)
    if "\\" not in statement:
        return forms
    backslash = _rewrite_tokens(statement, True, _BACKSLASH_TOKEN_RE)
    return forms + (_WHITESPACE_RE.sub(" ", backslash).strip(),)


def _removes_data(statement: str) -> bool:
    """
    Whether one statement removes something the schema cannot give back,
    by the rules `destructive_statements()` gives.
    """
    if isinstance(statement, MigrationStatement) and statement.destructive:
        return True
    return any(
        _DESTRUCTIVE_RE.search(form) or _ALTER_DROP_RE.search(form)
        for form in scannable_forms(statement)
    )


def destructive_statements(statements: Union[str, Sequence[str]]) -> List[str]:
    """
    Returns the statements that remove something the schema cannot give
    back: DROP TABLE, DROP COLUMN, DROP TYPE, DROP VIEW, DROP
    MATERIALIZED VIEW, DROP DATABASE, DROP SCHEMA ... CASCADE, TRUNCATE,
    DELETE (with or without FROM, and in a MERGE branch), a MySQL-style
    column drop that omits the COLUMN keyword (`ALTER TABLE t DROP col`),
    and constraint drops (DROP CONSTRAINT, DROP CHECK, DROP FOREIGN KEY).
    A dropped constraint removes no rows, but re-adding it needs the data
    to still satisfy it. A plain DROP SCHEMA refuses a schema that holds
    anything, so only the CASCADE form is labelled. Drops of indexes and
    keys are not labelled.

    A MigrationStatement marked `destructive`, such as a narrowing type
    change the diff generated, is labelled whatever its text says.

    Comments are removed and whitespace is collapsed, so each statement
    comes back on one line and a commented-out drop is not labelled. Both
    `--` and `/* */` comments are handled. The scan reads no text inside
    quotes or inside a dollar-quoted body, so a statement that names a
    drop in a string literal or a `$$` function body is not labelled.
    """
    if isinstance(statements, str):
        statements = [statements]
    return [normalize_statement(s) for s in statements if _removes_data(s)]


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
