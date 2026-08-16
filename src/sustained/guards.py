"""
Rules that read the statements a run would apply.

A guard takes the statement list and the dialect, and returns a verdict
for each statement it objects to. A `block` verdict stops the run before
any statement executes. A `warn` verdict prints and lets the run go on.

Guards are given to the migrator (`Migrator(..., guards=[...])`) or named
in the config module (`guards = [...]`) for the CLI. They run over every
statement the run would apply: file migrations, Python migrations with
SQL steps, and the diff against the models. A callable step renders no
SQL, so guards cannot read it, the same limit the destructive labels
carry.

The built-in rules are factories, so every one reads the same at the call
site:

    guards = [no_drops(), max_statements(50)]

The scan is textual, like the destructive labels: a rule matches on the
words in the statement and never parses SQL.
"""

from __future__ import annotations

import re
from typing import Callable, List, NamedTuple, Sequence

from sustained.analysis import normalize_statement
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


# A guard reads the statements a run would apply and returns its verdicts.
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


_DROP_RE = re.compile(r"\bDROP\s+(TABLE|COLUMN|VIEW|SCHEMA|DATABASE)\b", re.IGNORECASE)
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
_LOCK_TIMEOUT_RE = re.compile(r"\bSET\b.*\block_timeout\b", re.IGNORECASE)


def no_drops() -> Guard:
    """
    Blocks a statement that drops a table, a column, a view, a schema, or
    a database. Drops of constraints, indexes, and keys pass: they remove
    no data.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        found = []
        for statement in statements:
            text = normalize_statement(statement)
            if _DROP_RE.search(text) or _ALTER_DROP_RE.search(text):
                found.append(Verdict("no_drops", BLOCK, text))
        return found

    return guard


def index_must_be_concurrent() -> Guard:
    """
    Blocks `CREATE INDEX` without `CONCURRENTLY` on Postgres, where a
    plain index build holds a write lock on the table for its whole
    duration. Silent on every other dialect, which has no such keyword.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        if dialect is not Dialects.POSTGRES:
            return []
        found = []
        for statement in statements:
            text = normalize_statement(statement)
            if _CREATE_INDEX_RE.search(text) and not _CONCURRENTLY_RE.search(text):
                found.append(Verdict("index_must_be_concurrent", BLOCK, text))
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
            text = normalize_statement(statement)
            rewrites = bool(
                _TYPE_CHANGE_RE.search(text) or _SET_NOT_NULL_RE.search(text)
            )
            if not rewrites and _ADD_NOT_NULL_RE.search(text):
                rewrites = not _DEFAULT_RE.search(text)
            if rewrites:
                found.append(Verdict("no_table_rewrite", WARN, text))
        return found

    return guard


def no_lock_without_timeout() -> Guard:
    """
    Blocks a run that alters or drops a table without setting a lock
    timeout first. Without one, a statement waiting behind a long
    transaction queues every other query on that table behind it.

    The rule reads the whole run: one `SET lock_timeout` statement, in
    any migration of the run, satisfies it.
    """

    def guard(statements: Sequence[str], dialect: Dialects) -> List[Verdict]:
        texts = [normalize_statement(s) for s in statements]
        if any(_LOCK_TIMEOUT_RE.search(text) for text in texts):
            return []
        return [
            Verdict("no_lock_without_timeout", BLOCK, text)
            for text in texts
            if _LOCK_TAKING_RE.search(text)
        ]

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
