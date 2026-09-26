"""
The vocabulary an impact report is written in.

One portable vocabulary serves every engine. Each value is a `str`
enum, so it compares, prints, and serializes to JSON as its plain name.

- `Blocks`: what a lock stops other sessions doing on the table, from
  `nothing` to `reads_and_writes`. The engine's own name for the lock
  travels beside it, in `TableImpact.lock`.
- `Work`: what the statement does to the table's data, from `catalog`
  (metadata only) to `rewrite` (a copy of the table and its indexes).
- `Hold`: how long the lock is held: `brief`, the whole `statement`, or
  until the migration's `transaction` commits.
- `Evidence`: what the answer rests on: the rule alone (`static`), the
  rule with the live version, settings, and sizes (`catalog`), or what
  the server was seen to do (`observed`).
- `Confidence`: `known`, `likely` (the answer depends on something not
  read, which the finding names), or `unknown`.
- `Severity`: `info`, `warn`, or `danger`.

The ordered enums (`Blocks`, `Work`, `Severity`) rank their members, so
`max()` over a list of them picks the worst. `Work.UNKNOWN` ranks above
`Work.REWRITE`: a statement whose work is not known is treated as the
heaviest.

`Intent` is what a generated statement is meant to do, as the code that
generated it knows it. The diff and `DdlStep` rendering attach it to the
`MigrationStatement` they produce.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import List, Mapping, NamedTuple, Optional, Tuple

_NO_DETAILS: Mapping[str, object] = MappingProxyType({})


class _Ranked(str, Enum):
    """A str enum whose members rank in the order they are declared."""

    @property
    def rank(self) -> int:
        return list(type(self)).index(self)

    def __lt__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, _Ranked)
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, _Ranked)
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, _Ranked)
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        assert isinstance(other, _Ranked)
        return self.rank >= other.rank

    def __str__(self) -> str:
        return str(self.value)


class Blocks(_Ranked):
    """What a lock stops other sessions doing on a table, least first."""

    NOTHING = "nothing"
    DDL = "ddl"
    WRITES = "writes"
    READS_AND_WRITES = "reads_and_writes"


class Work(_Ranked):
    """What a statement does to a table's data, lightest first."""

    CATALOG = "catalog"
    SCAN = "scan"
    ROWS = "rows"
    INDEX_BUILD = "index_build"
    REWRITE = "rewrite"
    UNKNOWN = "unknown"


class Hold(_Ranked):
    """How long a lock is held, shortest first."""

    BRIEF = "brief"
    STATEMENT = "statement"
    TRANSACTION = "transaction"


class Evidence(_Ranked):
    """What an answer rests on, weakest first."""

    STATIC = "static"
    CATALOG = "catalog"
    OBSERVED = "observed"


class Confidence(_Ranked):
    """How sure an answer is, least sure first."""

    UNKNOWN = "unknown"
    LIKELY = "likely"
    KNOWN = "known"


class Severity(_Ranked):
    """How much a finding matters, least first."""

    INFO = "info"
    WARN = "warn"
    DANGER = "danger"


# Every operation kind an Intent may name. A generated statement carries
# one of these; hand-written SQL carries no intent at all.
INTENT_KINDS = frozenset(
    {
        "create_table",
        "drop_table",
        "rename_table",
        "rebuild_table",
        "add_column",
        "drop_column",
        "rename_column",
        "alter_column_type",
        "set_not_null",
        "drop_not_null",
        "set_column_default",
        "drop_column_default",
        "set_column_comment",
        "backfill",
        "add_foreign_key",
        "drop_foreign_key",
        "add_check",
        "add_unique",
        "drop_constraint",
        "create_index",
        "drop_index",
        "create_enum_type",
        "drop_enum_type",
        "add_enum_value",
        "session_setting",
    }
)


class Intent(NamedTuple):
    """
    What a generated statement is meant to do: an operation `kind` such
    as `add_column` or `alter_column_type`, the table it acts on, the
    column when there is one, and any further facts the generator knew
    in `details`, such as `from_type` and `to_type` for a type change.

    `table` is the dotted, unquoted name, `schema.table` when the table
    has a schema. It is None for an operation on no table, such as
    `create_enum_type`.
    """

    kind: str
    table: Optional[str]
    column: Optional[str] = None
    details: Mapping[str, object] = _NO_DETAILS

    def get(self, key: str, default: object = None) -> object:
        """One entry of `details`, or `default` when it is absent."""
        return self.details.get(key, default)


class Shape(NamedTuple):
    """
    What the recognizer understood of one statement: its `kind`, such as
    `create_index` or `alter_table`, the table it names, the actions of
    an ALTER TABLE in the order written, and the options the statement
    spelled, such as `concurrently`.

    A statement the recognizer cannot read in full has kind `unknown`,
    and never counts as understood.
    """

    kind: str
    table: Optional[str] = None
    actions: Tuple["Action", ...] = ()
    options: Mapping[str, object] = _NO_DETAILS

    @property
    def known(self) -> bool:
        return self.kind != UNKNOWN_SHAPE


class Action(NamedTuple):
    """
    One action inside an ALTER TABLE, such as `add_column` or
    `set_not_null`, with the column it names and its parsed options.
    """

    kind: str
    column: Optional[str] = None
    options: Mapping[str, object] = _NO_DETAILS


UNKNOWN_SHAPE = "unknown"


class TableImpact(NamedTuple):
    """
    What one statement does to one table: the engine's name for the lock
    it takes, what that lock blocks, the work it does, how long it holds
    the lock, and the table's size when a catalog read gave it. `rows`
    and `bytes` are estimates, and None when unknown.
    """

    table: str
    lock: Optional[str]
    blocks: Blocks
    work: Work
    hold: Hold
    rows: Optional[int] = None
    bytes: Optional[int] = None


class Finding(NamedTuple):
    """
    One thing a rule has to say about a statement. `rule` is the rule's
    id, such as `pg.create_index`. `remedy` holds the safer statements
    for this engine and version, in order, and is empty when none
    exists. `source` is the documentation the rule relies on.
    """

    rule: str
    severity: Severity
    message: str
    remedy: Tuple[str, ...] = ()
    source: Optional[str] = None


class StatementImpact(NamedTuple):
    """The impact of one statement, with what the answer rests on."""

    statement: str
    shape: Optional[Shape]
    tables: Tuple[TableImpact, ...]
    findings: Tuple[Finding, ...]
    evidence: Evidence
    confidence: Confidence

    @property
    def severity(self) -> Optional[Severity]:
        """The worst severity among the findings, or None with none."""
        return max((f.severity for f in self.findings), default=None)


class Lock(NamedTuple):
    """
    One lock a migration holds: the table, the engine's name for it,
    what it blocks, and the position of the statement that took it in
    the migration.
    """

    table: str
    lock: Optional[str]
    blocks: Blocks
    statement: int


class Window(NamedTuple):
    """
    How long a table stays blocked inside one migration. `blocks` is the
    worst lock held on the table, `taken_by` the position of the
    statement that took it, and `heaviest` the heaviest work that runs
    while it is held, by the statement at position `during`.
    """

    table: str
    blocks: Blocks
    taken_by: int
    heaviest: Work
    during: int


class MigrationImpact(NamedTuple):
    """
    The impact of one migration: its statements, the locks it holds,
    the windows those locks make, and findings about the migration as a
    whole, such as a deadlock risk.
    """

    migration_id: Optional[str]
    transactional: bool
    statements: Tuple[StatementImpact, ...]
    locks: Tuple[Lock, ...] = ()
    windows: Tuple[Window, ...] = ()
    findings: Tuple[Finding, ...] = ()


class ImpactReport(NamedTuple):
    """
    The impact of a run: one entry per migration, in run order, and what
    the report rests on. `profile` is the rule profile used, such as
    `postgres`, and `version` the server version the rules assumed.
    """

    profile: str
    version: Tuple[int, ...]
    evidence: Evidence
    migrations: Tuple[MigrationImpact, ...]

    @property
    def statements(self) -> Tuple[StatementImpact, ...]:
        """Every statement's impact, in run order."""
        return tuple(s for m in self.migrations for s in m.statements)

    @property
    def findings(self) -> Tuple[Finding, ...]:
        """Every finding, statement findings first within each migration."""
        found: List[Finding] = []
        for migration in self.migrations:
            for statement in migration.statements:
                found.extend(statement.findings)
            found.extend(migration.findings)
        return tuple(found)

    def count(self, severity: Severity) -> int:
        """How many findings carry the given severity."""
        return sum(1 for f in self.findings if f.severity is severity)
