"""
Rules that read the statements an up run would apply.

A guard takes the statement list and the dialect, and returns a verdict
for each statement it objects to. A `block` verdict stops the run before
any statement executes. A `warn` verdict prints and lets the run go on.

Guards are given to the migrator (`Migrator(..., guards=[...])`) or named
in the config module (`guards = [...]`) for the CLI. They run over every
statement an up run would apply: file migrations, Python migrations with
SQL steps, and the diff against the models. A callable step renders no
SQL, so guards cannot read it, the same limit the destructive labels
carry.

Down runs are not checked. A down undoes work that already passed the
rules, so a rule like `no_drops()` would block every rollback of a
create.

The built-in rules are factories, so every one reads the same at the call
site:

    guards = [no_drops(), max_statements(50)]

The scan is textual, like the destructive labels: a rule matches on the
words in the statement and never parses SQL. Comments and the text
inside quotes are kept out of the scan, so a rule reads neither a
commented-out drop nor a drop named in a string literal. The verdict
prints the statement with its literals intact.
"""

from __future__ import annotations

import re
from typing import Callable, List, NamedTuple, Sequence

from sustained.analysis import normalize_statement, scannable_statement
from sustained.dialects import Dialects

# The two verdicts a rule can return. There is no third severity: a rule
# either stops the run or tells the operator about it.
BLOCK = "block"
WARN = "warn"


class Verdict(NamedTuple):
    """
    One rule's objection to one statement: the rule that objected, whether
    it blocks or warns, and the statement it read.
    """

    rule: str
    verdict: str
    statement: str


# A guard reads the statements an up run would apply and returns its
# verdicts.
Guard = Callable[[Sequence[str], Dialects], List[Verdict]]


def run_guards(
    guards: Sequence[Guard], statements: Sequence[str], dialect: Dialects
) -> List[Verdict]:
    """
    Runs every guard over the statements and returns the verdicts, in
    guard order. An empty guard list returns an empty list, so a caller
    with no guards pays nothing.
    """
    verdicts: List[Verdict] = []
    for guard in guards:
        verdicts.extend(guard(statements, dialect))
    return verdicts


def blocking(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that stop a run."""
    return [v for v in verdicts if v.verdict == BLOCK]


def warnings_only(verdicts: Sequence[Verdict]) -> List[Verdict]:
    """The verdicts that only tell the operator."""
    return [v for v in verdicts if v.verdict == WARN]


_DROP_RE = re.compile(
    r"\bDROP\s+(TABLE|COLUMN|MATERIALIZED\s+VIEW|VIEW|SCHEMA|DATABASE|TYPE"
    r"|CONSTRAINT|CHECK|FOREIGN\s+KEY)\b",
    re.IGNORECASE,
)
# MySQL lets a column drop omit the COLUMN keyword.
_ALTER_DROP_RE = re.compile(
    r"\bALTER\s+TABLE\s+\S+\s+DROP\s+"
    r"(?!CONSTRAINT\b|INDEX\b|KEY\b|FOREIGN\b|PRIMARY\b|CHECK\b|PARTITION\b)"
    r"[A-Za-z_`\"\[]",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", re.IGNORECASE)
_CONCURRENTLY_RE = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)
_TYPE_CHANGE_RE = re.compile(
    r"\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b|\bMODIFY\s+(COLUMN\s+)?\S+\s",
    re.IGNORECASE,
)
_SET_NOT_NULL_RE = re.compile(r"\bSET\s+NOT\s+NULL\b", re.IGNORECASE)
_ADD_NOT_NULL_RE = re.compile(r"\bADD\s+(COLUMN\s+)?\S+.*\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_LOCK_TAKING_RE = re.compile(r"\bALTER\s+TABLE\b|\bDROP\s+TABLE\b", re.IGNORECASE)
# Only a statement that starts with `SET lock_timeout` counts, so an
# `UPDATE t SET lock_timeout = 5` on a column of that name does not pass
# for a timeout. `SESSION` and `LOCAL` are the two words Postgres allows
# between the two.
_LOCK_TIMEOUT_RE = re.compile(
    r"^SET\s+(SESSION\s+|LOCAL\s+)?lock_timeout\b", re.IGNORECASE
)


def no_drops() -> Guard:
    """
    Blocks a statement that drops a table, a column, a view, a
    materialized view, a schema, a database, an enum type, or a
    constraint. A dropped constraint removes no rows, but putting it back
    needs the data to still satisfy it, so the drop is not freely
    reversible. Drops of indexes and keys pass.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement in statements:
            scanned = scannable_statement(statement)
            if _DROP_RE.search(scanned) or _ALTER_DROP_RE.search(scanned):
                found.append(Verdict("no_drops", BLOCK, normalize_statement(statement)))
        return found

    return guard


def index_must_be_concurrent() -> Guard:
    """
    Blocks `CREATE INDEX` without `CONCURRENTLY` on Postgres, where a
    plain index build holds a write lock on the table for its whole
    duration. Silent on every other dialect, which has no such keyword.

    Postgres refuses CREATE INDEX CONCURRENTLY inside a transaction
    block, and the migrator wraps a migration in one. Put the index in a
    migration of its own with transactional=False, or in a SQL file that
    carries the '-- sustained: no transaction' marker. Such a migration
    that fails part way leaves an invalid index behind, which you drop by
    hand before you run it again.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        for statement in statements:
            scanned = scannable_statement(statement)
            if _CREATE_INDEX_RE.search(scanned) and not _CONCURRENTLY_RE.search(
                scanned
            ):
                found.append(
                    Verdict(
                        "index_must_be_concurrent",
                        BLOCK,
                        normalize_statement(statement),
                    )
                )
        return found

    return guard


def no_table_rewrite() -> Guard:
    """
    Warns on a statement that may rewrite the whole table: a column type
    change, or a NOT NULL added with no default to fill the existing
    rows.

    This rule warns where the others block. Whether a given change
    rewrites depends on the engine, its version, and whether the two
    types coerce, so a block here would stop safe statements. Read the
    warning against your own engine.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement in statements:
            scanned = scannable_statement(statement)
            rewrites = bool(
                _TYPE_CHANGE_RE.search(scanned) or _SET_NOT_NULL_RE.search(scanned)
            )
            if not rewrites and _ADD_NOT_NULL_RE.search(scanned):
                rewrites = not _DEFAULT_RE.search(scanned)
            if rewrites:
                found.append(
                    Verdict("no_table_rewrite", WARN, normalize_statement(statement))
                )
        return found

    return guard


def no_lock_without_timeout() -> Guard:
    """
    Blocks a run that alters or drops a table without setting a lock
    timeout first, on Postgres, where a statement waiting behind a long
    transaction queues every other query on that table behind it. Silent
    on every other dialect, which has no such setting.

    The rule reads the statements in run order. A `SET lock_timeout`
    covers the statements after it, and none before it, so a timeout at
    the end of a run no longer excuses an ALTER at the start.

    A guard reads a flat statement list and cannot see where one
    migration ends and the next begins. `SET LOCAL` dies at the commit
    that ends its migration, so a `SET LOCAL` in an early migration still
    counts for a later one, which the server would not honour. Write
    `SET lock_timeout` without LOCAL to set it for the session, or repeat
    the `SET LOCAL` in each migration that needs it.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        covered = False
        for statement in statements:
            scanned = scannable_statement(statement)
            if _LOCK_TIMEOUT_RE.search(scanned):
                covered = True
            elif not covered and _LOCK_TAKING_RE.search(scanned):
                found.append(
                    Verdict(
                        "no_lock_without_timeout",
                        BLOCK,
                        normalize_statement(statement),
                    )
                )
        return found

    return guard


def max_statements(limit: int) -> Guard:
    """
    Blocks a run longer than `limit` statements, which usually means
    several changes that should have been several deploys. The verdict
    names every statement past the limit.
    """
    if limit < 1:
        raise ValueError("max_statements needs a limit of at least 1.")
    rule = f"max_statements({limit})"

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        return [
            Verdict(rule, BLOCK, normalize_statement(statement))
            for statement in statements[limit:]
        ]

    return guard
