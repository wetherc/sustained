"""
Explicit, ordered schema migrations.

A Migration pairs an id with an up step and an optional down step. Steps
are a SQL string, a list of SQL strings, or a callable that receives the
connection. The Migrator applies pending migrations in order, records each
applied id in a tracking table, and reverts through the down steps.

The tracking table stores one row per applied migration: the id, a
monotonic sequence number, a SHA-256 checksum of the up step, the apply
timestamp, the execution time in milliseconds, and a success flag.
Tracking tables written by earlier versions of Sustained, which held only
the id and the timestamp, are upgraded in place on first use.

A second table holds rehearsal rows: one row per set of statements a
rehearsal proved, keyed to the applied history it started from. A run that
would remove data reads that table first and stops when nothing covers it.

Migrations are written by hand, generated from a model with
create_table_migration(), or produced by schema diffing through
sustained.autogenerate and Migrator.up(models=[...]).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import warnings
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from sustained.ddl import DdlStep
from sustained.dialects import Dialects
from sustained.execution import cursor_scope, pinned_transaction, transaction
from sustained.types import Connection, Cursor, SqlValue

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter
    from sustained.autogenerate import IntrospectedTable
    from sustained.compilers.base import Compiler
    from sustained.guards import Guard, Verdict
    from sustained.model import Model
    from sustained.schema import ColumnDef, TableOptions

CallbackTarget = Union[Connection, "AsyncAdapter"]
"""What a step or a callback is handed: the connection for Migrator, the
adapter for AsyncMigrator."""

# A callback returns nothing, or an awaitable the async migrator awaits.
CallbackResult = Optional[Awaitable[None]]

MigrationStep = Union[
    str,
    DdlStep,
    Sequence[Union[str, DdlStep]],
    Callable[[CallbackTarget], CallbackResult],
]


class _DeriveDown:
    """
    The default for Migration's down parameter: derive it from the up
    step when the up step is all reversible ddl steps, and use None
    otherwise. Passing down=None explicitly declares the migration
    irreversible instead.
    """


_DERIVE = _DeriveDown()


def _step_elements(step: MigrationStep) -> Optional[List[Union[str, DdlStep]]]:
    """A step's statements as a list, or None for a callable step."""
    if isinstance(step, (str, DdlStep)):
        return [step]
    if callable(step):
        return None
    return list(step)


def _default_compiler() -> "Compiler":
    return Dialects.get_compiler(Dialects.DEFAULT)


def _render_elements(
    elements: List[Union[str, DdlStep]], compiler: Optional["Compiler"]
) -> List[str]:
    """The SQL statements a step's elements run on one dialect."""
    statements: List[str] = []
    for element in elements:
        if isinstance(element, DdlStep):
            statements.extend(element.render(compiler or _default_compiler()))
        else:
            statements.append(element)
    return statements


def _derived_down(
    migration_id: str, up: MigrationStep
) -> Optional[List[Union[str, DdlStep]]]:
    """
    The down step a ddl up step implies: the inverses, newest first.
    A step that cannot reverse refuses the derivation; the migration
    then needs an explicit down step or an explicit down=None. An up
    step with no ddl steps in it derives nothing, as before.
    """
    elements = _step_elements(up)
    if elements is None or not any(isinstance(e, DdlStep) for e in elements):
        return None
    blockers = [
        (e.op if isinstance(e, DdlStep) else "a raw SQL string")
        for e in elements
        if not (isinstance(e, DdlStep) and e.reversible)
    ]
    if blockers:
        raise ValueError(
            f"Migration '{migration_id}' cannot derive its down step: "
            f"{', '.join(blockers)} does not reverse. Pass an explicit "
            "down step, or down=None to declare the migration "
            "irreversible."
        )
    inverses: List[Union[str, DdlStep]] = []
    for element in reversed(elements):
        assert isinstance(element, DdlStep)
        inverse = element.inverse()
        assert inverse is not None
        inverses.append(inverse)
    return inverses


class Callbacks(NamedTuple):
    """
    The functions a migrator calls around a run.

    `before_migrate` runs before anything else, including validation and
    the advisory lock. `after_migrate` runs after a successful run that
    applied at least one migration, and receives the applied ids; a run
    that applied nothing does not call it. `on_error` receives the failed
    migration's id, or None when the run failed before any migration ran,
    and the error, which then propagates.

    The first argument of each is the connection the migrator runs on, or
    the adapter for AsyncMigrator. An async migrator awaits a callback
    that returns an awaitable.
    """

    before_migrate: Optional[Callable[[CallbackTarget], CallbackResult]] = None
    after_migrate: Optional[Callable[[CallbackTarget, List[str]], CallbackResult]] = (
        None
    )
    on_error: Optional[
        Callable[[CallbackTarget, Optional[str], BaseException], CallbackResult]
    ] = None


class Migration:
    """
    One schema change with an id, an up step, and an optional down step.

    A checksum may be supplied for callable steps, whose SQL cannot be
    hashed; validation then compares it like a computed one.

    A repeatable migration re-runs whenever its checksum changes, for
    views, functions, and seed data. Repeatables have no down step and
    run after every versioned migration.

    When the up step is a list of reversible ddl steps and no down is
    given, the down step derives itself: the inverses of the up steps,
    newest first. A ddl step that cannot reverse (a drop, add_enum_value,
    raw sql()) refuses the derivation; pass an explicit down step, or
    down=None to declare the migration irreversible. Repeatables never
    derive a down step.
    """

    def __init__(
        self,
        id: str,
        up: MigrationStep,
        down: Union[Optional[MigrationStep], _DeriveDown] = _DERIVE,
        checksum: Optional[str] = None,
        repeatable: bool = False,
    ) -> None:
        if not id:
            raise ValueError("A migration needs a non-empty id.")
        if isinstance(down, _DeriveDown):
            down = None if repeatable else _derived_down(id, up)
        if repeatable and down is not None:
            raise ValueError(
                f"Repeatable migration '{id}' cannot have a down step; "
                "repeatables re-run instead of reverting."
            )
        if repeatable and callable(up) and checksum is None:
            raise ValueError(
                f"Repeatable migration '{id}' has a callable step; pass an "
                "explicit checksum so re-runs can be detected."
            )
        self.id = id
        self.up = up
        self.down = down
        self.checksum = checksum
        self.repeatable = repeatable


def migration_checksum(migration: Migration) -> Optional[str]:
    """
    The SHA-256 hex digest of a migration's up statements, each stripped of
    surrounding whitespace. Callable steps have no SQL to hash and return
    the migration's explicit checksum, or None when it has none. A ddl
    step hashes as its canonical signature rather than its rendered SQL,
    so the checksum stays the same on every dialect.
    """
    if migration.checksum is not None:
        return migration.checksum
    elements = _step_elements(migration.up)
    if elements is None:
        return None
    digest = hashlib.sha256()
    for element in elements:
        if isinstance(element, DdlStep):
            digest.update(element.signature().encode("utf-8"))
        else:
            digest.update(element.strip().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class AppliedRecord(NamedTuple):
    """
    One row of the tracking table. `generated` marks a migration written
    by the diff against the models rather than registered by hand, which
    is why nothing on disk carries its id.
    """

    id: str
    seq: Optional[int]
    checksum: Optional[str]
    success: bool
    generated: bool = False


class RehearsalResult(NamedTuple):
    """
    What a rehearsal proved about one migration.

    `up_ok` reports whether the up step ran. `down_ok` reports whether the
    down step ran, or None when nothing was proved, and `error` then says
    why: the migration has no down step, the sweep never reached it, or it
    is a repeatable, which has no down step to prove. When a step raised,
    `error` holds the database error.

    `landed` and `reversed` carry what the schema itself said. Each is
    None when it was not checked, an empty list when it was checked and
    proved, and a list of readable lines when it failed. `landed` is only
    checked for the migration generated from the models, since a
    hand-written migration may create objects no model declares.
    `reversed` compares the schema after the down sweep against the
    snapshot taken before the rehearsal, so it is shared by every
    migration in the run: a leftover object names the whole sweep, not
    one step of it. It stays None unless every step in the run reversed,
    since changes a migration without a down step leaves behind cannot be
    charged to the steps that did come back.
    """

    id: str
    up_ok: bool
    down_ok: Optional[bool]
    error: Optional[str]
    landed: Optional[List[str]] = None
    reversed: Optional[List[str]] = None


def rehearsal_failed(result: RehearsalResult) -> bool:
    """
    Whether one result stops a rehearsal from passing: a step that raised,
    a down step that failed, models that did not land, or a schema that
    did not come back. A down step that could not be proved is not a
    failure.
    """
    return (
        not result.up_ok
        or result.down_ok is False
        or bool(result.landed)
        or bool(result.reversed)
    )


class Rehearsal(List[RehearsalResult]):
    """
    A rehearsal's results, one per migration that ran, plus the row it
    earned.

    The class is a list, so it iterates and indexes like the plain list
    earlier versions returned. `key` names the exact content the rehearsal
    covered: the applied history it started from and the statements it
    ran. `recorded` says whether the row reached the tracking
    database, which a scratch rehearsal leaves to the caller.
    """

    def __init__(
        self,
        results: Iterable[RehearsalResult],
        key: str,
        recorded: bool = False,
    ) -> None:
        super().__init__(results)
        self.key = key
        self.recorded = recorded

    @property
    def ok(self) -> bool:
        """True when every result passed."""
        return not any(rehearsal_failed(r) for r in self)


# The outcomes a rehearsal row can hold. 'override' marks statements that
# were applied with unrehearsed=True: nothing proved them, and the row is
# there so the database says who skipped the proof and when.
REHEARSAL_PASSED = "passed"
REHEARSAL_FAILED = "failed"
REHEARSAL_OVERRIDE = "override"


def _rehearsal_token(checksum: Optional[str], migration_id: str) -> str:
    """
    One entry in a rehearsal key. A callable step has no SQL to hash and no
    explicit checksum, so its id stands in: the token keeps a mixed set
    ordered and hashable, and a callable can never trigger the gate on its
    own, since the destructive scan cannot read it either.
    """
    return checksum if checksum is not None else f"id:{migration_id}"


def checked_unique_ids(migrations: Sequence[Migration]) -> None:
    """
    Raises when two migrations carry the same id. Both migrators call it
    before they keep the list, since an ambiguous id makes every status,
    target, and tracking row ambiguous too.
    """
    counts = Counter(m.id for m in migrations)
    duplicates = sorted(i for i, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate migration ids: {duplicates}.")


class Digest(Protocol):
    """The part of a hashlib hash the rehearsal keys use."""

    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...

    def copy(self) -> "Digest": ...


def _applied_digest(applied: Sequence[AppliedRecord]) -> Digest:
    """
    A digest holding the applied history and ready for a run's migrations.
    Prefix keys copy it instead of hashing the history again per prefix.
    """
    digest = hashlib.sha256()
    digest.update(b"applied\n")
    for record in applied:
        if not record.success:
            continue
        digest.update(_rehearsal_token(record.checksum, record.id).encode("utf-8"))
        digest.update(b"\n")
    digest.update(b"run\n")
    return digest


def _digest_migration(digest: Digest, migration: Migration) -> None:
    """Adds one migration's token to a digest built by _applied_digest()."""
    token = _rehearsal_token(migration_checksum(migration), migration.id)
    digest.update(token.encode("utf-8"))
    digest.update(b"\n")


def rehearsal_key(applied: Sequence[AppliedRecord], run: Sequence[Migration]) -> str:
    """
    The SHA-256 hex digest that names one rehearsal: the checksums of the
    successful tracking rows the run starts from, then the checksums of
    the migrations it runs.

    The applied history is part of the key because a rehearsal proves a
    set of statements against one starting schema. A database with a
    different history must not accept the row.

    Ids are not hashed, only statements, so a generated migration that
    takes a new timestamped id between the rehearsal and the run keeps the
    same key.
    """
    digest = _applied_digest(applied)
    for migration in run:
        _digest_migration(digest, migration)
    return digest.hexdigest()


def _destructive_in(
    run: Sequence[Migration], compiler: Optional["Compiler"] = None
) -> List[Tuple[str, str]]:
    """
    The (migration id, statement) pairs in a run that remove data. A
    callable step renders no SQL and is invisible here, the same limit the
    plan command's labels carry. Ddl steps render for the given compiler's
    dialect, so the labels read the SQL the run would run.
    """
    from sustained.analysis import destructive_statements

    found: List[Tuple[str, str]] = []
    for migration in run:
        if callable(migration.up):
            continue
        statements = migration_sql(migration, "up", compiler)
        for statement in destructive_statements(statements):
            found.append((migration.id, statement))
    return found


def _destructive_prefix_keys(
    applied: Sequence[AppliedRecord],
    pending: Sequence[Migration],
    compiler: Optional["Compiler"] = None,
) -> List[str]:
    """
    The keys a targeted run would look for.

    up(target=...) applies the versioned migrations up to the target and
    skips the repeatables, so its run set is a prefix of the versioned
    pending list. A rehearsal applied every one of those prefixes on its
    way up and took them all back on the way down, so it proved them too.

    Only prefixes that remove data get a key: nothing else ever reads the
    rehearsal table, and a row per prefix on every rehearsal would be waste.

    The keys are built one migration at a time. Rendering each prefix's
    SQL and hashing the whole history again per prefix cost the square of
    the pending count; copying a running digest and carrying a running
    destructive flag costs one render and one hash per migration.
    """
    versioned = [m for m in pending if not m.repeatable]
    digest = _applied_digest(applied)
    keys = []
    destructive = False
    for migration in versioned:
        _digest_migration(digest, migration)
        # A prefix removes data as soon as one of its migrations does, so
        # the flag never goes back and the rest need no rendering.
        destructive = destructive or bool(_destructive_in([migration], compiler))
        if destructive:
            keys.append(digest.copy().hexdigest())
    return keys


def run_statements(
    run: Sequence[Migration], compiler: Optional["Compiler"] = None
) -> List[str]:
    """
    Every up statement a run would apply, in order. Callable steps render
    no SQL and are skipped, so a guard cannot read them, the same limit
    the destructive labels carry. Ddl steps render for the given
    compiler's dialect, which is why the guards can read them.
    """
    statements: List[str] = []
    for migration in run:
        if callable(migration.up):
            continue
        statements.extend(migration_sql(migration, "up", compiler))
    return statements


def _report_warnings(verdicts: Sequence["Verdict"]) -> None:
    """Prints the warning verdicts on stderr, one per line."""
    for verdict in verdicts:
        print(f"warn: {verdict.rule}  {verdict.statement}", file=sys.stderr)


def check_guards(
    guards: Sequence["Guard"],
    run: Sequence[Migration],
    dialect: Dialects,
    reported: Optional[Set["Verdict"]] = None,
) -> None:
    """
    Runs the guards over the statements a run would apply. A blocking
    verdict raises GuardBlocked before anything executes; warnings print
    on stderr and the run goes on.

    `reported` collects the warnings already printed. A run whose
    statements are known in two parts checks the whole set twice, and the
    set keeps the operator from reading the same warning twice.
    """
    from sustained.exceptions import GuardBlocked
    from sustained.guards import blocking, run_guards, warnings_only

    if not guards:
        return
    compiler = Dialects.get_compiler(dialect)
    verdicts = run_guards(guards, run_statements(run, compiler), dialect)
    blockers = blocking(verdicts)
    if blockers:
        raise GuardBlocked(blockers)
    warned = warnings_only(verdicts)
    if reported is not None:
        warned = [v for v in warned if v not in reported]
        reported.update(warned)
    _report_warnings(warned)


def _call_on_error(
    callbacks: Callbacks, connection: CallbackTarget, error: BaseException
) -> None:
    """
    Hands a failed run to the on_error callback. A callback that raises
    must not replace the error it was told about, so its own failure is
    reported on stderr and set aside. before_migrate and after_migrate
    are called plainly: a failure there is the operator's own and stops
    the run.
    """
    if callbacks.on_error is None:
        return
    try:
        callbacks.on_error(connection, getattr(error, "migration_id", None), error)
    except Exception as callback_error:
        print(f"error: on_error raised {callback_error!r}", file=sys.stderr)


def _rehearsal_message(
    destructive: List[Tuple[str, str]],
    outcome: Optional[str],
    target: Optional[str] = None,
) -> str:
    """
    Why a run stopped, which statements stopped it, and the two ways
    forward. A failed rehearsal reads differently from no rehearsal at
    all: the operator has already seen these statements break.

    A targeted run gets the target back in the suggested command, so
    copying the line runs what was blocked and not the whole set.
    """
    target_sql = f" --target {target}" if target is not None else ""
    if outcome == REHEARSAL_FAILED:
        opening = (
            "The last rehearsal of these statements failed, and this run "
            "removes data:"
        )
    else:
        opening = (
            "This run removes data, and no rehearsal has proved these " "statements:"
        )
    width = max(len(migration_id) for migration_id, _ in destructive)
    lines = [f"  {migration_id:<{width}}  {sql}" for migration_id, sql in destructive]
    return "\n".join(
        [opening]
        + lines
        + [
            "Prove them first: sustained rehearse",
            "Or apply them without proof: sustained migrate"
            f"{target_sql} --unrehearsed",
        ]
    )


# Dialects whose schema changes roll back, so a rehearsal can undo itself.
# The others need a scratch database; see Migrator.rehearse(). DEFAULT is
# on the list for SQLite, the engine the generic compiler usually serves;
# a config that leaves the dialect unset while pointing at MySQL, whose
# DDL commits as it runs, should declare Dialects.MYSQL so the refusal
# arrives before the run instead of after it.
_REHEARSABLE = frozenset({Dialects.DEFAULT, Dialects.POSTGRES, Dialects.DUCKDB})


def _check_rehearsable(dialect: Dialects) -> None:
    """
    Refuses to rehearse where a rollback would not take the schema back.
    """
    if dialect in _REHEARSABLE:
        return
    raise ValueError(
        f"rehearse needs a database whose schema changes roll back, and "
        f"{dialect.name.lower()} is not on that list "
        f"({', '.join(sorted(d.name.lower() for d in _REHEARSABLE))}). "
        "Point rehearse at a scratch database instead: pass scratch=True "
        "on a throwaway connection, or define get_rehearsal_connection() "
        "in the config module when running the CLI."
    )


# Columns added when upgrading a tracking table written by an earlier
# version, which held only id and applied_at.
_UPGRADE_COLUMNS = (
    "seq",
    "checksum",
    "execution_ms",
    "success",
    "generated",
    "steps",
)


def quoted_columns(compiler: "Compiler", *names: str) -> str:
    """
    Tracking table column names, quoted for the dialect. Quoting is not
    cosmetic here: `generated` is a reserved word on MySQL, so a bare
    reference to it is a syntax error.
    """
    return ", ".join(compiler.quote_identifier(name) for name in names)


def records_select(compiler: "Compiler", table_sql: str) -> str:
    """Reads every tracking table row in application order."""
    columns = quoted_columns(compiler, "id", "seq", "checksum", "success", "generated")
    order = quoted_columns(compiler, "seq", "applied_at", "id")
    return f"SELECT {columns} FROM {table_sql} ORDER BY {order}"


def insert_sql(compiler: "Compiler", table_sql: str) -> str:
    """Writes one tracking table row."""
    columns = quoted_columns(
        compiler,
        "id",
        "seq",
        "checksum",
        "applied_at",
        "execution_ms",
        "success",
        "generated",
        "steps",
    )
    values = ", ".join([compiler.placeholder()] * 8)
    return f"INSERT INTO {table_sql} ({columns}) VALUES ({values})"


def update_sql(compiler: "Compiler", table_sql: str) -> str:
    """Rewrites the tracking table row a repeatable already has."""
    placeholder = compiler.placeholder()
    column = compiler.quote_identifier
    assignments = ", ".join(
        f"{column(name)} = {placeholder}"
        for name in (
            "checksum",
            "applied_at",
            "execution_ms",
            "success",
            "generated",
            "steps",
        )
    )
    return (
        f"UPDATE {table_sql} SET {assignments} " f"WHERE {column('id')} = {placeholder}"
    )


def _tracking_column_defs(constraints: bool) -> Dict[str, "ColumnDef"]:
    """
    The tracking table's columns. Engines without constraints, such as
    Athena, get plain nullable columns; the migrator never writes a
    duplicate id.
    """
    from sustained.schema import Boolean, Integer, String, Text

    if constraints:
        return {
            "id": String(255, primary_key=True),
            "seq": Integer(),
            "checksum": String(64),
            "applied_at": Text(nullable=False),
            "execution_ms": Integer(),
            "success": Boolean(nullable=False),
            "generated": Boolean(),
            "steps": Text(),
        }
    return {
        "id": String(255),
        "seq": Integer(),
        "checksum": String(64),
        "applied_at": Text(),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
        "steps": Text(),
    }


def _rehearsal_column_defs(constraints: bool) -> Dict[str, "ColumnDef"]:
    """
    The rehearsal table's columns: the key a rehearsal earned, what it
    proved, and when. Engines without constraints get plain nullable
    columns, as the tracking table does.
    """
    from sustained.schema import String, Text

    if constraints:
        return {
            "rehearsal_key": String(64, primary_key=True),
            "outcome": String(16, nullable=False),
            "rehearsed_at": Text(nullable=False),
        }
    return {
        "rehearsal_key": String(64),
        "outcome": String(16),
        "rehearsed_at": Text(),
    }


def _upgrade_column_def(name: str) -> "ColumnDef":
    """A nullable definition for one upgrade column, safe to ADD COLUMN."""
    from sustained.schema import Boolean, Integer, String, Text

    defs: Dict[str, "ColumnDef"] = {
        "seq": Integer(),
        "checksum": String(64),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
        "steps": Text(),
    }
    return defs[name]


def create_table_migration(model: Type["Model"]) -> Migration:
    """
    Builds a migration that creates the model's table from its tableColumns
    on the way up and drops it on the way down, enum types included on
    dialects that have them. The migration id is 'create_<tableName>'.
    """
    return Migration(
        id=f"create_{model.tableName}",
        up=model.create_table_statements(),
        down=model.drop_table_statements(),
    )


def migration_sql(
    migration: Migration,
    direction: str = "up",
    compiler: Optional["Compiler"] = None,
) -> List[str]:
    """
    Renders a migration's statements for offline review. Callable steps
    cannot be rendered and appear as a comment. Ddl steps render for the
    given compiler's dialect, or ANSI when none is given.
    """
    step = migration.up if direction == "up" else migration.down
    if step is None:
        raise ValueError(f"Migration '{migration.id}' has no {direction} step.")
    elements = _step_elements(step)
    if elements is None:
        return [f"-- migration '{migration.id}': callable step, run online"]
    return _render_elements(elements, compiler)


def _run_step(
    connection: Connection, step: MigrationStep, compiler: Optional["Compiler"] = None
) -> None:
    elements = _step_elements(step)
    if elements is None:
        assert callable(step)
        step(connection)
        return
    with cursor_scope(connection) as cursor:
        for statement in _render_elements(elements, compiler):
            cursor.execute(statement)


def _next_seq(records: List[AppliedRecord]) -> int:
    return 1 + max((r.seq or 0 for r in records), default=0)


def _is_current(
    record: Optional[AppliedRecord],
    current_checksum: Optional[str],
    repeatable: bool,
) -> bool:
    """True when the tracking row makes a run unnecessary."""
    if record is None or not record.success:
        return False
    if not repeatable:
        return True
    return record.checksum == current_checksum


def _migration_state(record: Optional[AppliedRecord], migration: Migration) -> str:
    """One migration's state: 'applied', 'pending', or 'changed'."""
    if record is None or not record.success:
        return "pending"
    if migration.repeatable and record.checksum != migration_checksum(migration):
        return "changed"
    return "applied"


def _down_sweep(ran: List[Migration]) -> Iterator[Tuple[Migration, Optional[str]]]:
    """
    The order a rehearsal runs its down steps in, newest first, paired with
    the reason a migration cannot be proved, or None when its down step
    should run.

    A repeatable has no down step and never blocks the sweep. A versioned
    migration without one does block it: everything older sits under
    changes that cannot be taken back, so their down steps cannot run
    either.
    """
    blocked: Optional[str] = None
    for migration in reversed(ran):
        if migration.repeatable:
            yield migration, "no down step (repeatable)"
        elif blocked is not None:
            yield migration, f"down not reached: '{blocked}' has no down step"
        elif migration.down is None:
            blocked = migration.id
            yield migration, "no down step"
        else:
            yield migration, None


def _reversal_provable(
    ran: List[Migration],
    outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]],
) -> bool:
    """
    Whether comparing the schema after the down sweep against the one
    before it proves anything.

    It does when at least one down step ran and every versioned migration
    in the run reversed. A versioned migration whose down step did not run
    leaves its own changes in the database, and blaming those on the steps
    that did reverse would report a rehearsal that behaved as designed as
    a failure. Repeatables are left out of the requirement: they never
    have a down step, so waiting for one would switch the comparison off
    for every run that carries a view or a seed.
    """
    if not any(down_ok is True for down_ok, _ in outcomes.values()):
        return False
    return all(
        outcomes.get(m.id, (None, None))[0] is True for m in ran if not m.repeatable
    )


def _rehearsal_results(
    ran: List[Migration],
    up_error: Optional[Tuple[str, str]],
    down_outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]],
    landed: Optional[Dict[str, List[str]]] = None,
    reverted: Optional[List[str]] = None,
) -> List[RehearsalResult]:
    """
    Merges the up and down outcomes into one result per migration, in the
    order the up steps ran. `up_error` is the (id, message) pair of the
    migration that stopped the rehearsal, if one did.

    `landed` holds the outstanding differences per migration id, for the
    migrations whose landing was checked. `reverted` holds the schema
    left over after the down sweep, and goes on every migration whose
    down step ran, since one sweep proves them together.
    """
    landed = landed or {}
    unfinished: Tuple[Optional[bool], Optional[str]] = (
        None,
        "down not rehearsed: the run stopped",
    )
    results = [
        RehearsalResult(
            m.id,
            True,
            *down_outcomes.get(m.id, unfinished),
            landed=landed.get(m.id),
            reversed=(
                reverted if down_outcomes.get(m.id, unfinished)[0] is True else None
            ),
        )
        for m in ran
    ]
    if up_error is not None:
        results.append(RehearsalResult(up_error[0], False, None, up_error[1]))
    return results


def _tag_migration(error: BaseException, migration_id: str) -> None:
    """
    Records which migration raised on the exception itself, so a caller
    that catches it can name the migration. The CLI reads it when it hands
    a failure to the config module's on_error callback, and when it prints
    the error. An exception type that rejects new attributes, such as one
    with __slots__, keeps its error unmarked rather than masking it.
    """
    try:
        setattr(error, "migration_id", migration_id)
    except Exception:
        pass


def _stored_steps(
    migration: Migration, generated: bool, compiler: Optional["Compiler"] = None
) -> Optional[str]:
    """
    The JSON a generated migration's tracking row carries: its up and down
    statements, so a later process can revert it.

    A registered migration stores nothing. Its statements live in the
    migration list or the migrations directory, and the checksum on the
    row already says whether they changed since. A generated migration has
    no such home: the diff produced it, applied it, and the process ended.
    A callable step cannot be stored, and the diff never produces one.
    The row stores rendered SQL, so ddl steps render for the given
    compiler's dialect, the one the run executed.
    """
    if not generated or callable(migration.up):
        return None
    down = (
        None if migration.down is None else migration_sql(migration, "down", compiler)
    )
    return json.dumps({"up": migration_sql(migration, "up", compiler), "down": down})


def _restore_migration(migration_id: str, steps: Optional[str]) -> Optional[Migration]:
    """
    The migration a generated tracking row describes, or None when the row
    carries no statements: a row written before this column existed, or
    one for a registered migration.
    """
    if not steps:
        return None
    try:
        stored = json.loads(steps)
    except ValueError:
        return None
    return Migration(migration_id, up=stored["up"], down=stored["down"])


def _tag_applied(error: BaseException, applied: List[str]) -> None:
    """
    Records which migrations were already applied when a run stopped, on
    the exception itself, so a caller can report them. The gates that run
    against the generated migration read a schema the registered
    migrations already changed, so a block there is not a block on an
    untouched database. An exception type that rejects new attributes,
    such as one with __slots__, keeps its error unmarked.
    """
    if not applied:
        return
    try:
        setattr(error, "applied", list(applied))
    except Exception:
        pass


def _validation_problems(
    migrations: List[Migration],
    records: List[AppliedRecord],
    allow_out_of_order: bool = False,
    require_registered: bool = True,
) -> List[str]:
    """
    Compares the registered migrations against the tracking table rows and
    describes every inconsistency: failed attempts, applied migrations the
    migrator does not know, edited migrations whose checksum no longer
    matches, and pending migrations ordered before applied ones. Passing
    require_registered=False skips the unknown-id check.

    A row marked generated is never reported as unknown. It was written
    by a diff against the models, so no file or list carries its id, and
    a later run regenerates whatever difference is left.
    """
    problems: List[str] = []
    registered = {m.id: m for m in migrations}
    applied_ids = {r.id for r in records if r.success}

    for record in records:
        if not record.success:
            problems.append(
                f"migration '{record.id}' has a failed attempt on record; "
                "clean up any partial changes, then run repair() and retry"
            )
    for record in records:
        if not record.success:
            continue
        migration = registered.get(record.id)
        if migration is None:
            if require_registered and not record.generated:
                problems.append(
                    f"applied migration '{record.id}' is not registered "
                    "with this migrator"
                )
            continue
        if migration.repeatable:
            # A changed checksum is the re-run signal, not a problem.
            continue
        current = migration_checksum(migration)
        if (
            current is not None
            and record.checksum is not None
            and current != record.checksum
        ):
            problems.append(
                f"checksum mismatch for '{record.id}': the migration "
                "changed after it was applied; restore it, or run repair() "
                "to accept the new contents"
            )
    if not allow_out_of_order:
        first_pending: Optional[str] = None
        for migration in migrations:
            if migration.repeatable:
                continue
            if migration.id not in applied_ids:
                if first_pending is None:
                    first_pending = migration.id
            elif first_pending is not None:
                problems.append(
                    f"pending migration '{first_pending}' is ordered before "
                    f"applied migration '{migration.id}'; pass "
                    "allow_out_of_order=True to apply it anyway"
                )
                break
    return problems


class Migrator:
    """
    Applies and reverts an ordered list of migrations on one connection.

    Applied migration ids live in a tracking table, created on first use.
    Each migration runs inside a transaction, so a failing step leaves the
    schema at the previous migration. Engines that do not support
    transactional DDL may still leave partial changes from a multi-step
    migration. Engines without transactions at all, such as Athena, run
    each step bare; a failing migration there can leave partial changes
    that need manual cleanup.

    `guards` are rules that read the statements a run would apply; see
    sustained.guards. A blocking verdict stops up() before any statement
    runs. `callbacks` are the functions to call around a run.
    """

    def __init__(
        self,
        connection: Connection,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
        tracking_table_options: Optional["TableOptions"] = None,
        rehearsal_table: str = "sustained_rehearsals",
        guards: Optional[Sequence["Guard"]] = None,
        callbacks: Optional[Callbacks] = None,
    ) -> None:
        checked_unique_ids(migrations)
        self._guards = list(guards or [])
        self._callbacks = callbacks or Callbacks()
        self._connection = connection
        self._migrations = list(migrations)
        self._table = table
        self._rehearsal_table = rehearsal_table
        self._dialect = dialect
        self._compiler: "Compiler" = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False
        self._rehearsal_ready = False
        self._rehearsing = False

    @property
    def connection(self) -> Connection:
        """The connection this migrator runs on."""
        return self._connection

    @property
    def dialect(self) -> Dialects:
        """The dialect this migrator compiles for."""
        return self._dialect

    @property
    def compiler(self) -> "Compiler":
        """The compiler that renders this migrator's ddl steps."""
        return self._compiler

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    def _table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._table)

    def _own_tables(self) -> Tuple[str, ...]:
        """
        The tables Sustained keeps for itself. A diff against the models
        leaves them alone, and a rehearsal snapshot drops them, so its own
        bookkeeping never reads as drift or as an object left behind.
        """
        return (self._table, self._rehearsal_table)

    @contextmanager
    def _migration_scope(self) -> Iterator[None]:
        """
        A transaction on engines whose schema changes roll back; a bare run
        followed by a commit (when the driver has one) on engines whose do
        not.

        A rehearsal opens one transaction around the whole run and rolls it
        back at the end, so each migration runs bare and nothing commits.
        """
        if self._rehearsing:
            yield
            return
        if self._compiler.supports_transactional_ddl():
            with transaction(self._connection, self._dialect):
                yield
            return
        yield
        self._commit_quietly()

    def _execute(
        self, cursor: "Cursor", sql: str, params: Tuple[SqlValue, ...]
    ) -> None:
        """Runs one parameterized statement, adapted for the dialect."""
        cursor.execute(*self._compiler.prepare_execution(sql, params))

    def _run_sql(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One statement on a cursor of its own, given back when it finishes.
        A cursor left open holds its result set, and pyodbc and the MySQL
        drivers refuse the next statement on the connection once enough of
        those pile up.
        """
        with closing(self._connection.cursor()) as cursor:
            self._execute(cursor, sql, params)

    def _write_tracking_row(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One tracking table write inside the migration's own transaction.
        It runs on the transaction's cursor where a block is open, so a
        failed migration takes its row back with it on the engines that
        roll DDL back, and on DuckDB, where every fresh cursor is a session
        of its own.
        """
        with cursor_scope(self._connection) as cursor:
            self._execute(cursor, sql, params)

    def _commit_quietly(self) -> None:
        if hasattr(self._connection, "commit"):
            self._connection.commit()

    def _rollback_quietly(self) -> None:
        try:
            if hasattr(self._connection, "rollback"):
                self._connection.rollback()
        except Exception:
            pass

    @contextmanager
    def _lock_scope(self) -> Iterator[None]:
        """
        Holds the engine's advisory lock, named after the tracking table,
        for the duration of a run, so concurrent migrators queue instead of
        racing. A no-op on engines without one.
        """
        lock_statements = self._compiler.migration_lock_sql(self._table)
        if not lock_statements:
            yield
            return
        with closing(self._connection.cursor()) as cursor:
            for statement in lock_statements:
                cursor.execute(statement)
        try:
            yield
        finally:
            for statement in self._compiler.migration_unlock_sql(self._table):
                try:
                    with closing(self._connection.cursor()) as cursor:
                        cursor.execute(statement)
                except Exception:
                    pass

    def _ensure_tracking_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._tracking_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._table_ddl_sql(),
            _tracking_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql)
        self._commit_quietly()
        self._upgrade_tracking_table()
        self._tracking_ready = True

    def _rehearsal_table_sql(self) -> str:
        return self._compiler.quote_identifier(self._rehearsal_table)

    def _rehearsal_table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._rehearsal_table)

    def _ensure_rehearsal_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._rehearsal_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._rehearsal_table_ddl_sql(),
            _rehearsal_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql)
        self._commit_quietly()
        self._rehearsal_ready = True

    def record_rehearsal(self, key: str, outcome: str = REHEARSAL_PASSED) -> None:
        """
        Writes the row for one rehearsal key, replacing any earlier row
        for the same key. The outcome is 'passed', 'failed', or 'override'
        for statements applied with unrehearsed=True.

        A rehearsal on a scratch database records nothing on its own: the
        row belongs on the database the next run will read. Call this
        on a migrator bound to that database once the scratch run passes.
        """
        if outcome not in (REHEARSAL_PASSED, REHEARSAL_FAILED, REHEARSAL_OVERRIDE):
            raise ValueError(
                f"Unknown rehearsal outcome {outcome!r}; use "
                f"{REHEARSAL_PASSED!r}, {REHEARSAL_FAILED!r}, or "
                f"{REHEARSAL_OVERRIDE!r}."
            )
        self._ensure_rehearsal_table()
        placeholder = self._compiler.placeholder()
        table = self._rehearsal_table_sql()
        self._run_sql(
            f"DELETE FROM {table} WHERE rehearsal_key = {placeholder}", (key,)
        )
        values = ", ".join([placeholder] * 3)
        self._run_sql(
            f"INSERT INTO {table} (rehearsal_key, outcome, rehearsed_at) "
            f"VALUES ({values})",
            (key, outcome, datetime.now(timezone.utc).isoformat()),
        )
        self._commit_quietly()

    def rehearsal_outcome(self, key: str) -> Optional[str]:
        """
        What the recorded rehearsal of this key proved: 'passed', 'failed',
        or None when no rehearsal has covered it.
        """
        self._ensure_rehearsal_table()
        placeholder = self._compiler.placeholder()
        with closing(self._connection.cursor()) as cursor:
            self._execute(
                cursor,
                f"SELECT outcome FROM {self._rehearsal_table_sql()} "
                f"WHERE rehearsal_key = {placeholder}",
                (key,),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def rehearsed(self, key: str) -> bool:
        """True when a passing rehearsal covers this key."""
        return self.rehearsal_outcome(key) == REHEARSAL_PASSED

    def _has_columns(self, columns: Tuple[str, ...]) -> bool:
        """Probes the tracking table for the given columns."""
        try:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(
                    f"SELECT {quoted_columns(self._compiler, *columns)} "
                    f"FROM {self._table_sql()} WHERE 1 = 0"
                )
                cursor.fetchall()
            return True
        except Exception:
            # A failed probe can poison an open transaction (Postgres
            # aborts it), so clear the slate before the next statement.
            self._rollback_quietly()
            return False

    def _upgrade_tracking_table(self) -> None:
        """
        Brings a tracking table written by an earlier version, which held
        only id and applied_at, up to the current shape. Missing columns
        are added nullable; seq and success are backfilled from the
        existing rows in applied order.
        """
        from sustained.schema import render_column_sql

        if self._has_columns(_UPGRADE_COLUMNS):
            return
        added: List[str] = []
        for name in _UPGRADE_COLUMNS:
            if self._has_columns((name,)):
                continue
            column_sql = render_column_sql(
                self._compiler, name, _upgrade_column_def(name), inline_pk=False
            )
            statement = self._compiler.compile_add_column(
                self._table_ddl_sql(), column_sql
            )
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(statement)
            added.append(name)
        self._commit_quietly()
        placeholder = self._compiler.placeholder()
        # Backfill only the columns this run added, and only where they are
        # still null, so values a partial earlier upgrade wrote survive.
        column = self._compiler.quote_identifier
        if "success" in added:
            self._run_sql(
                f"UPDATE {self._table_sql()} SET {column('success')} = "
                f"{placeholder} WHERE {column('success')} IS NULL",
                (True,),
            )
        if "seq" in added:
            with closing(self._connection.cursor()) as cursor:
                cursor.execute(
                    f"SELECT {column('id')} FROM {self._table_sql()} "
                    f"ORDER BY {quoted_columns(self._compiler, 'applied_at', 'id')}"
                )
                ids = [row[0] for row in cursor.fetchall()]
            for position, migration_id in enumerate(ids, start=1):
                self._run_sql(
                    f"UPDATE {self._table_sql()} SET {column('seq')} = "
                    f"{placeholder} WHERE {column('id')} = {placeholder} "
                    f"AND {column('seq')} IS NULL",
                    (position, migration_id),
                )
        self._commit_quietly()

    def applied_records(self) -> List[AppliedRecord]:
        """Returns every tracking table row in application order."""
        self._ensure_tracking_table()
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(records_select(self._compiler, self._table_sql()))
            return [
                AppliedRecord(row[0], row[1], row[2], bool(row[3]), bool(row[4]))
                for row in cursor.fetchall()
            ]

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in self.applied_records() if r.success]

    def _versioned(self) -> List[Migration]:
        return [m for m in self._migrations if not m.repeatable]

    def _repeatables(self) -> List[Migration]:
        return [m for m in self._migrations if m.repeatable]

    def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        records = {r.id: r for r in self.applied_records()}
        result = [
            m
            for m in self._versioned()
            if not _is_current(records.get(m.id), migration_checksum(m), False)
        ]
        result.extend(
            m
            for m in self._repeatables()
            if not _is_current(records.get(m.id), migration_checksum(m), True)
        )
        return result

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def statuses(self) -> List[tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        records = {r.id: r for r in self.applied_records()}
        return [
            (m.id, _migration_state(records.get(m.id), m)) for m in self._migrations
        ]

    def _insert_sql(self) -> str:
        return insert_sql(self._compiler, self._table_sql())

    def _update_sql(self) -> str:
        return update_sql(self._compiler, self._table_sql())

    def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        from sustained.exceptions import MigrationError

        problems = _validation_problems(self._migrations, self.applied_records())
        if problems and raise_on_problems:
            raise MigrationError(problems)
        return problems

    def repair(self) -> List[str]:
        """
        Brings the tracking table back in line with the registered
        migrations: deletes rows left by failed attempts and rewrites
        stored checksums that no longer match, including null checksums on
        rows written before checksums existed. Returns a description of
        every action taken. Schema changes a failed attempt left behind
        are not touched; clean those up first.

        Repeatables keep their stored checksums. For them a changed
        checksum schedules a re-run, and rewriting the row here would
        cancel that run without the new contents ever reaching the
        database.
        """
        records = self.applied_records()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        actions: List[str] = []
        for record in records:
            if not record.success:
                self._run_sql(
                    f"DELETE FROM {self._table_sql()} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder} "
                    f"AND {self._compiler.quote_identifier('success')} = "
                    f"{self._compiler.compile_boolean(False)}",
                    (record.id,),
                )
                actions.append(f"removed the failed attempt of '{record.id}'")
                continue
            migration = by_id.get(record.id)
            if migration is None or migration.repeatable:
                continue
            current = migration_checksum(migration)
            if current is not None and current != record.checksum:
                self._run_sql(
                    f"UPDATE {self._table_sql()} SET "
                    f"{self._compiler.quote_identifier('checksum')} = "
                    f"{placeholder} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder}",
                    (current, record.id),
                )
                actions.append(f"updated the stored checksum of '{record.id}'")
        self._commit_quietly()
        return actions

    def _record_failure(
        self,
        migration: Migration,
        seq: int,
        update: bool = False,
        generated: bool = False,
    ) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine whose schema changes do not roll back, where partial changes
        may remain. A repeatable that already has a row updates it in
        place. A failure to write the row never masks the original error. A
        rehearsal writes nothing: its whole run rolls back.
        """
        if self._rehearsing or self._compiler.supports_transactional_ddl():
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            checksum = migration_checksum(migration)
            if update:
                self._run_sql(
                    self._update_sql(),
                    (
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        _stored_steps(migration, generated, self._compiler),
                        migration.id,
                    ),
                )
            else:
                self._run_sql(
                    self._insert_sql(),
                    (
                        migration.id,
                        seq,
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        _stored_steps(migration, generated, self._compiler),
                    ),
                )
            self._commit_quietly()
        except Exception:
            pass

    def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        unrehearsed: bool = False,
    ) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.

        The run validates first: failed attempts, unknown applied ids,
        checksum mismatches, and out-of-order pending migrations all stop
        it. Pass validate=False to skip the checks, or
        allow_out_of_order=True to accept a pending migration that is
        ordered before an applied one.

        Repeatables run after the versioned migrations, whenever their
        checksum is new or changed. A targeted run skips them: a
        repeatable may depend on a versioned migration past the target,
        and the next full up() runs it. The target must name a versioned
        migration.

        With models, the run applies the versioned migrations first, then
        diffs the models against the database and applies the generated
        migration, then the repeatables. The diff is taken after the
        pending migrations have run, so it sees the schema they left and
        never regenerates a table one of them just created. Additive
        changes generate reversible steps, so down() takes them back.
        Drops need allow_drops=True and do not reverse. A generated
        migration always runs last of the versioned ones, so it cannot be
        combined with a target, and the remaining arguments are the diff
        options plan() takes.

        A run that would remove data stops unless a passing rehearsal
        covers exactly these statements against exactly this applied
        history. Rehearse first, or pass unrehearsed=True to apply them
        without the proof, which writes an 'override' row naming what
        was applied unproved. Runs that only add are never gated, and a
        callable step is invisible to the check, the same limit the
        destructive labels carry.

        The migrator's guards read the statements before they run. A
        blocking verdict raises GuardBlocked; a warning prints on stderr.
        Both gates read the registered migrations before anything runs,
        and read them again together with the generated migration, whose
        statements exist only once the registered ones have applied. A
        block or a missing row at that second reading leaves the
        registered migrations applied, and lists their ids on the
        exception's `applied` attribute. The migrator's callbacks fire
        around the run.
        """
        callbacks = self._callbacks
        if callbacks.before_migrate is not None:
            callbacks.before_migrate(self._connection)
        try:
            applied = self._run_up(
                target=target,
                validate=validate,
                allow_out_of_order=allow_out_of_order,
                models=models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
                unrehearsed=unrehearsed,
            )
        except Exception as error:
            _call_on_error(callbacks, self._connection, error)
            raise
        if applied and callbacks.after_migrate is not None:
            callbacks.after_migrate(self._connection, applied)
        return applied

    def _run_up(
        self,
        target: Optional[str],
        validate: bool,
        allow_out_of_order: bool,
        models: Optional[List[Type["Model"]]],
        allow_drops: bool,
        ignore_changed_columns: bool,
        migration_id: Optional[str],
        renames: Optional[dict[str, str]],
        table_renames: Optional[dict[str, str]],
        type_casts: Optional[dict[str, str]],
        unrehearsed: bool,
    ) -> List[str]:
        """The run itself, without the callbacks up() wraps it in."""
        from sustained.exceptions import MigrationError

        if models is not None and target is not None:
            raise ValueError(
                "up() cannot take both models and a target: the generated "
                "migration always runs last, so a target would leave it out."
            )
        require_registered = models is None

        with self._lock_scope():
            if models is not None:
                self._ensure_tracking_table()

            migrations = self._versioned()
            if target is not None:
                ids = [m.id for m in migrations]
                if target not in ids:
                    if any(m.id == target for m in self._repeatables()):
                        raise ValueError(
                            f"Migration target {target!r} is repeatable; a "
                            "target must name a versioned migration."
                        )
                    raise ValueError(f"Unknown migration target: {target!r}.")
                migrations = migrations[: ids.index(target) + 1]

            records = self.applied_records()
            if validate:
                problems = _validation_problems(
                    self._migrations,
                    records,
                    allow_out_of_order,
                    require_registered=require_registered,
                )
                if problems:
                    raise MigrationError(problems)
            records_by_id = {r.id: r for r in records}
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            applied_now: List[str] = []
            versioned_now = [m for m in migrations if m.id not in already_applied]
            repeatables_now = [
                m
                for m in (self._repeatables() if target is None else [])
                if not _is_current(records_by_id.get(m.id), migration_checksum(m), True)
            ]
            # The registered set is checked before anything runs. The
            # order matches pending(), so a rehearsal of the same set
            # produces the same key.
            registered_run = versioned_now + repeatables_now
            warned: Set["Verdict"] = set()
            check_guards(self._guards, registered_run, self._dialect, warned)
            self._require_rehearsal_row(records, registered_run, unrehearsed, target)
            final_run = list(registered_run)
            for migration in versioned_now:
                self._apply(migration, next_seq, update=False)
                next_seq += 1
                applied_now.append(migration.id)
            if models is not None:
                generated = self.plan(
                    models,
                    allow_drops=allow_drops,
                    ignore_changed_columns=ignore_changed_columns,
                    migration_id=migration_id,
                    renames=renames,
                    table_renames=table_renames,
                    type_casts=type_casts,
                )
                if generated is not None:
                    # The generated statements are known only now, after
                    # the registered migrations left the schema they diff
                    # against, so both gates run a second time before the
                    # one migration they could not see. The registered
                    # migrations are already applied and committed by
                    # then, so a block here reports what it stopped after.
                    final_run = registered_run + [generated]
                    try:
                        check_guards(self._guards, final_run, self._dialect, warned)
                        self._require_rehearsal_row(
                            records, final_run, unrehearsed, target
                        )
                    except Exception as error:
                        _tag_applied(error, applied_now)
                        raise
                    self._migrations.append(generated)
                    self._apply(generated, next_seq, update=False, generated=True)
                    next_seq += 1
                    applied_now.append(generated.id)
            for migration in repeatables_now:
                record = records_by_id.get(migration.id)
                self._apply(migration, next_seq, update=record is not None)
                if record is None:
                    next_seq += 1
                applied_now.append(migration.id)
            if unrehearsed and _destructive_in(final_run, self._compiler):
                # The proof was waived, so the row says so. It never
                # unlocks a later run: only 'passed' does that.
                self.record_rehearsal(
                    rehearsal_key(records, final_run), REHEARSAL_OVERRIDE
                )
            return applied_now

    def _require_rehearsal_row(
        self,
        records: List[AppliedRecord],
        run: List[Migration],
        unrehearsed: bool,
        target: Optional[str] = None,
    ) -> None:
        """
        Stops a run that removes data unless a passing rehearsal covers
        exactly this content. A run that only adds passes straight
        through and never reads the rehearsal table.
        """
        from sustained.exceptions import RehearsalRequired

        if unrehearsed:
            return
        destructive = _destructive_in(run, self._compiler)
        if not destructive:
            return
        outcome = self.rehearsal_outcome(rehearsal_key(records, run))
        if outcome == REHEARSAL_PASSED:
            return
        raise RehearsalRequired(_rehearsal_message(destructive, outcome, target))

    def _apply(
        self, migration: Migration, seq: int, update: bool, generated: bool = False
    ) -> None:
        """
        Runs one migration's up step and records it: an INSERT for a
        first run, an UPDATE in place when a repeatable re-runs, keeping
        its original seq. `generated` marks a migration the diff against
        the models produced, whose id nothing on disk carries.
        """
        try:
            with self._migration_scope():
                started = time.perf_counter()
                _run_step(self._connection, migration.up, self._compiler)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                timestamp = datetime.now(timezone.utc).isoformat()
                checksum = migration_checksum(migration)
                if update:
                    self._write_tracking_row(
                        self._update_sql(),
                        (
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            _stored_steps(migration, generated, self._compiler),
                            migration.id,
                        ),
                    )
                else:
                    self._write_tracking_row(
                        self._insert_sql(),
                        (
                            migration.id,
                            seq,
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            _stored_steps(migration, generated, self._compiler),
                        ),
                    )
        except Exception as error:
            self._record_failure(migration, seq, update=update, generated=generated)
            _tag_migration(error, migration.id)
            raise

    def rehearse(
        self,
        scratch: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> Rehearsal:
        """
        Runs every pending migration up, then back down, inside one
        transaction, and rolls that transaction back. Returns one result
        per migration that ran; an empty result means nothing was pending.

        A rehearsal proves that the SQL is valid, that the schema moved,
        and that the down steps take it back. It does not prove anything
        about data on a production-sized table. The tracking table is
        created if it does not exist yet; nothing else survives. A
        callable step that commits on its own is the exception: that
        commit cannot be taken back.

        With models, the run rehearses what up(models=[...]) would apply:
        the generated migration joins the pending list for this run only,
        and its result reports whether the schema then matched the models.
        The remaining arguments are the diff options up() takes, and they
        should match the ones the real run will use.

        The up steps run in the order up() runs them: the versioned
        migrations, then the generated one, then the repeatables. The down
        steps then run newest-first, skipping the repeatables, which have
        none. The first migration without a down step stops the sweep,
        since everything older sits under changes that cannot be taken
        back. The first step that raises stops the rehearsal.

        The schema is read before the run and again after the down sweep.
        A difference between the two means a down step ran without taking
        its change back. The comparison is only made when every step in
        the run reversed: one migration without a down step leaves changes
        that no other step can be blamed for. Tables and columns are
        compared; indexes, constraints, and defaults are not, so a
        leftover index is not reported yet.

        A passing run leaves a rehearsal row behind, keyed to the applied
        history it started from and the statements it ran, which up() reads
        before it applies anything that removes data. A failing run records
        the failure under the same key. A scratch rehearsal records
        nothing, since the row belongs on the database the next run
        will read; the key comes back on the result for the caller to
        record there.

        Only dialects whose schema changes roll back may rehearse. Pass
        scratch=True when the connection points at a database that can be
        thrown away: the dialect check is then skipped, and the changes may
        survive the rollback.
        """
        from sustained.execution import in_transaction

        if not scratch:
            _check_rehearsable(self._dialect)
        if getattr(self._connection, "autocommit", False) is True:
            raise ValueError(
                "rehearse cannot run on a connection in autocommit mode: "
                "nothing would roll back. Open the connection without "
                "autocommit, or point rehearse at a scratch database."
            )
        if in_transaction(self._connection):
            raise ValueError(
                "rehearse cannot run inside an open transaction() block: "
                "its rollback would take the caller's work back too."
            )
        # The lock sits outside the rehearsal transaction, so the rollback
        # runs before the lock is released. The state reads sit inside it,
        # so a concurrent migrator cannot apply between the read and the
        # rehearsal.
        with self._lock_scope():
            self.validate()
            pending = self.pending()
            record_list = self.applied_records()
            if not pending and models is None:
                return Rehearsal([], rehearsal_key(record_list, []))
            if models is not None:
                self._ensure_tracking_table()
            records = {r.id: r for r in record_list}
            seq = _next_seq(record_list)
            before = self._snapshot()
            # What the row will cover: the pending set, plus the
            # generated migration once the diff produces one.
            attempted: List[Migration] = list(pending)
            has_drift = False
            # Close whatever transaction the reads above opened, so the
            # rehearsal's BEGIN starts a fresh one instead of warning.
            self._rollback_quietly()
            self._rehearsing = True
            # The rehearsal's statements share this transaction's cursor,
            # so on an engine that gives every cursor its own session they
            # land in the transaction the rollback below takes back.
            with pinned_transaction(self._connection, self._dialect):
                try:
                    ran: List[Migration] = []
                    up_error: Optional[Tuple[str, str]] = None

                    def apply_each(group: List[Migration]) -> None:
                        nonlocal seq, up_error
                        for migration in group:
                            try:
                                self._apply(
                                    migration, seq, update=migration.id in records
                                )
                            except Exception as error:
                                up_error = (migration.id, str(error))
                                return
                            seq += 1
                            ran.append(migration)

                    # The order matches up(): the versioned migrations, then
                    # the generated one, then the repeatables, which may read
                    # objects the generated migration creates.
                    apply_each([m for m in pending if not m.repeatable])
                    landed: Dict[str, List[str]] = {}
                    if models is not None and up_error is None:
                        # The diff is taken here, inside the rehearsal, so it
                        # sees the schema the pending migrations just left. The
                        # generated migration joins the run without being
                        # registered: nothing outside the rehearsal should see a
                        # migration the rollback is about to take back.
                        drift = self.plan(
                            models,
                            allow_drops=allow_drops,
                            ignore_changed_columns=ignore_changed_columns,
                            migration_id=migration_id,
                            renames=renames,
                            table_renames=table_renames,
                            type_casts=type_casts,
                        )
                        if drift is not None:
                            attempted.append(drift)
                            has_drift = True
                            try:
                                self._apply(drift, seq, update=False, generated=True)
                            except Exception as error:
                                up_error = (drift.id, str(error))
                            else:
                                seq += 1
                                ran.append(drift)
                                # The renames have already run, so the schema
                                # holds the new names. Passing the hints again
                                # would ask to rename objects that are gone.
                                landed[drift.id] = self.drift(
                                    models,
                                    ignore_changed_columns=ignore_changed_columns,
                                )
                    if up_error is None:
                        apply_each([m for m in pending if m.repeatable])
                    outcomes = {} if up_error else self._rehearse_down(ran)
                    reverted = None
                    if before is not None and _reversal_provable(ran, outcomes):
                        from sustained.autogenerate import diff_snapshots

                        after = self._snapshot()
                        if after is not None:
                            reverted = diff_snapshots(before, after)
                    results = _rehearsal_results(
                        ran, up_error, outcomes, landed, reverted
                    )
                finally:
                    self._rehearsing = False
                    self._roll_back_rehearsal()
            # The rehearsal row is written after the rollback, in its own
            # committed transaction, and still inside the lock: everything
            # the rehearsal itself wrote has just been taken back.
            key = rehearsal_key(record_list, attempted)
            passed = not any(rehearsal_failed(r) for r in results)
            recorded = False
            if not scratch:
                self.record_rehearsal(
                    key, REHEARSAL_PASSED if passed else REHEARSAL_FAILED
                )
                if passed and has_drift:
                    # A run without models applies the registered
                    # migrations and stops there, which this rehearsal
                    # also proved.
                    self.record_rehearsal(rehearsal_key(record_list, pending))
                if passed:
                    for prefix_key in _destructive_prefix_keys(
                        record_list, pending, self._compiler
                    ):
                        self.record_rehearsal(prefix_key)
                recorded = True
            return Rehearsal(results, key, recorded)

    def _snapshot(self) -> Optional[Dict[str, "IntrospectedTable"]]:
        """
        The live schema, without Sustained's own tables, or None when the
        database will not report it. A rehearsal compares two of these,
        and the tracking and rehearsal tables are created by the rehearsal
        itself, so leaving them in would report them as objects left
        behind.

        A read that raises leaves the rehearsal's other proofs standing
        and reports the comparison as not checked, which is what a
        scratch database on an engine Sustained cannot introspect gives.
        """
        from sustained.autogenerate import introspect_schema

        try:
            schema = introspect_schema(self._connection, self._dialect)
        except Exception:
            return None
        for name in self._own_tables():
            schema.pop(name.lower(), None)
        return dict(schema)

    def drift(
        self,
        models: List[Type["Model"]],
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        ignore_changed_columns: bool = False,
    ) -> List[str]:
        """
        What the models still ask for, one readable line each, empty when
        the database holds everything they declare.

        Objects the database holds and the models do not are left out. A
        generated migration leaves those alone unless drops are allowed,
        so a schema built partly by hand does not read as drift here. Use
        plan() for the full comparison, drops included.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a run that generates its migration the same
        way.
        """
        from sustained.autogenerate import diff_schema

        diff = diff_schema(
            self._connection,
            models,
            dialect=self._dialect,
            exclude_tables=self._own_tables(),
            renames=renames,
            table_renames=table_renames,
        )
        return diff.outstanding(ignore_changed_columns=ignore_changed_columns)

    def _roll_back_rehearsal(self) -> None:
        """
        Takes back everything the rehearsal did. The statement runs first,
        on the rehearsal's own cursor, because a driver's own rollback()
        call does nothing on connections that never opened a transaction of
        their own; the driver call follows to leave its bookkeeping
        straight.
        """
        statement = self._compiler.rollback_transaction_sql()
        if statement is not None:
            try:
                with cursor_scope(self._connection) as cursor:
                    cursor.execute(statement)
            except Exception:
                pass
        self._rollback_quietly()

    def _rehearse_down(
        self, ran: List[Migration]
    ) -> Dict[str, Tuple[Optional[bool], Optional[str]]]:
        """
        Runs the down steps of a rehearsal, newest-first, and reports what
        each one proved. A step that raises stops the sweep; the
        migrations under it report that they were not reached.
        """
        placeholder = self._compiler.placeholder()
        outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]] = {}
        failed: Optional[str] = None
        for migration, reason in _down_sweep(ran):
            if failed is not None:
                outcomes[migration.id] = (
                    None,
                    f"down not reached: '{failed}' down failed",
                )
            elif reason is not None:
                outcomes[migration.id] = (None, reason)
            else:
                try:
                    with self._migration_scope():
                        _run_step(
                            self._connection,
                            cast(MigrationStep, migration.down),
                            self._compiler,
                        )
                        self._run_sql(
                            f"DELETE FROM {self._table_sql()} WHERE "
                            f"{self._compiler.quote_identifier('id')} = "
                            f"{placeholder}",
                            (migration.id,),
                        )
                except Exception as error:
                    outcomes[migration.id] = (False, str(error))
                    failed = migration.id
                else:
                    outcomes[migration.id] = (True, None)
        return outcomes

    def baseline(self, target: str) -> List[str]:
        """
        Marks registered migrations up to and including the target as
        applied without running them, for adopting a database whose schema
        already matches. Rows are written with real checksums and a null
        execution time; already-applied migrations are skipped. Returns the
        ids that were recorded.

        The target must name a versioned migration. Every repeatable is
        recorded at its current checksum, so the first migrate after
        adoption does not re-run objects the schema already holds.
        """
        versioned = self._versioned()
        ids = [m.id for m in versioned]
        if target not in ids:
            if any(m.id == target for m in self._repeatables()):
                raise ValueError(
                    f"Migration target {target!r} is repeatable; a target "
                    "must name a versioned migration."
                )
            raise ValueError(f"Unknown migration target: {target!r}.")
        with self._lock_scope():
            records = self.applied_records()
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            recorded: List[str] = []
            for migration in versioned[: ids.index(target) + 1] + self._repeatables():
                if migration.id in already_applied:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                self._run_sql(
                    self._insert_sql(),
                    (
                        migration.id,
                        next_seq,
                        migration_checksum(migration),
                        timestamp,
                        None,
                        True,
                        False,
                        None,
                    ),
                )
                next_seq += 1
                recorded.append(migration.id)
            self._commit_quietly()
            return recorded

    def plan(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        ignore_undeclared: bool = True,
    ) -> Optional[Migration]:
        """
        Diffs the database against the models and returns the migration
        up(models=[...]) would generate, without registering or applying
        it. Returns None when the schema is already up to date. The
        tracking table is excluded from the diff.

        Objects the models do not declare are left alone, since a
        database may hold tables that hand-written migrations created.
        Pass allow_drops=True to generate the drops instead, or
        ignore_undeclared=False to refuse to generate while they exist.
        """
        from sustained.autogenerate import autogenerate

        generated_id = migration_id or datetime.now(timezone.utc).strftime(
            "auto_%Y%m%d%H%M%S_%f"
        )
        return autogenerate(
            self._connection,
            models,
            id=generated_id,
            dialect=self._dialect,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            exclude_tables=self._own_tables(),
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            ignore_undeclared=ignore_undeclared,
        )

    def sync(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> List[str]:
        """
        Deprecated since 2.13.0, removed in 3.0: call
        up(models=[...]) instead, which does the same work under the verb
        the CLI and the docs already use.
        """
        warnings.warn(
            "Migrator.sync() is deprecated and will be removed in 3.0. "
            "Call up(models=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.up(
            models=models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
        )

    def script(self, direction: str = "up") -> str:
        """
        Renders the SQL a run would execute, without executing anything,
        for review or DBA handoff. 'up' renders every pending migration;
        'down' renders the applied migrations newest-first. Tracking table
        bookkeeping statements are included.
        """
        placeholder_free_ts = datetime.now(timezone.utc).isoformat()
        format_value = self._compiler.format_value
        column = self._compiler.quote_identifier
        insert_columns = quoted_columns(
            self._compiler,
            "id",
            "seq",
            "checksum",
            "applied_at",
            "execution_ms",
            "success",
        )
        lines: List[str] = []
        if direction == "up":
            records = self.applied_records()
            records_by_id = {r.id: r for r in records}
            applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            for migration in self._versioned():
                if migration.id in applied:
                    continue
                lines.append(f"-- up: {migration.id}")
                lines.extend(
                    f"{s};" for s in migration_sql(migration, "up", self._compiler)
                )
                lines.append(
                    f"INSERT INTO {self._table_sql()} "
                    f"({insert_columns}) "
                    f"VALUES ({format_value(migration.id)}, {next_seq}, "
                    f"{format_value(migration_checksum(migration))}, "
                    f"{format_value(placeholder_free_ts)}, NULL, "
                    f"{self._compiler.compile_boolean(True)});"
                )
                next_seq += 1
            for migration in self._repeatables():
                record = records_by_id.get(migration.id)
                checksum = migration_checksum(migration)
                if _is_current(record, checksum, True):
                    continue
                lines.append(f"-- repeat: {migration.id}")
                lines.extend(
                    f"{s};" for s in migration_sql(migration, "up", self._compiler)
                )
                if record is None:
                    lines.append(
                        f"INSERT INTO {self._table_sql()} "
                        f"({insert_columns}) "
                        f"VALUES ({format_value(migration.id)}, {next_seq}, "
                        f"{format_value(checksum)}, "
                        f"{format_value(placeholder_free_ts)}, NULL, "
                        f"{self._compiler.compile_boolean(True)});"
                    )
                    next_seq += 1
                else:
                    lines.append(
                        f"UPDATE {self._table_sql()} "
                        f"SET {column('checksum')} = {format_value(checksum)}, "
                        f"{column('applied_at')} = "
                        f"{format_value(placeholder_free_ts)}, "
                        f"{column('execution_ms')} = NULL, "
                        f"{column('success')} = "
                        f"{self._compiler.compile_boolean(True)} "
                        f"WHERE {column('id')} = {format_value(migration.id)};"
                    )
        elif direction == "down":
            by_id = {m.id: m for m in self._migrations}
            for migration_id in reversed(self._applied_versioned()):
                registered = by_id.get(migration_id)
                if registered is None or registered.down is None:
                    lines.append(
                        f"-- down: {migration_id} has no reversible step; stopping"
                    )
                    break
                lines.append(f"-- down: {migration_id}")
                lines.extend(
                    f"{s};" for s in migration_sql(registered, "down", self._compiler)
                )
                lines.append(
                    f"DELETE FROM {self._table_sql()} WHERE {column('id')} = "
                    f"{format_value(migration_id)};"
                )
        else:
            raise ValueError("direction must be 'up' or 'down'.")
        return "\n".join(lines)

    def _applied_versioned(self) -> List[str]:
        """Applied ids with the repeatables left out; down() skips them."""
        repeatable_ids = {m.id for m in self._repeatables()}
        return [i for i in self.applied() if i not in repeatable_ids]

    def down_to(self, target: str) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted.
        """
        applied = self._applied_versioned()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return self.down(steps) if steps else []

    def _generated_migration(self, migration_id: str) -> Optional[Migration]:
        """
        The migration a generated tracking row describes, read back from
        the row itself, or None when the row holds no statements.

        up(models=[...]) applies a migration that exists nowhere but that
        run, so a later process has nothing to revert it with. The row
        carries the statements for exactly that reason.
        """
        placeholder = self._compiler.placeholder()
        with closing(self._connection.cursor()) as cursor:
            self._execute(
                cursor,
                f"SELECT {self._compiler.quote_identifier('steps')} "
                f"FROM {self._table_sql()} WHERE "
                f"{self._compiler.quote_identifier('id')} = {placeholder}",
                (migration_id,),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return _restore_migration(migration_id, str(row[0]))

    def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step. Repeatables are never
        reverted. Returns the ids that were reverted.

        A migration generated from the models is reverted from its own
        tracking row, which holds the statements it ran, so a process that
        never saw the diff can still take it back. Every other migration
        must be registered with this migrator.
        """
        with self._lock_scope():
            self._ensure_tracking_table()
            applied = self._applied_versioned()
            by_id = {m.id: m for m in self._migrations}
            placeholder = self._compiler.placeholder()
            reverted: List[str] = []
            for migration_id in reversed(applied[-steps:] if steps else []):
                migration = by_id.get(migration_id) or self._generated_migration(
                    migration_id
                )
                if migration is None:
                    raise ValueError(
                        f"Applied migration '{migration_id}' is not registered "
                        "with this migrator; cannot revert."
                    )
                if migration.down is None:
                    raise ValueError(f"Migration '{migration_id}' has no down step.")
                with self._migration_scope():
                    _run_step(self._connection, migration.down, self._compiler)
                    self._write_tracking_row(
                        f"DELETE FROM {self._table_sql()} WHERE "
                        f"{self._compiler.quote_identifier('id')} = "
                        f"{placeholder}",
                        (migration_id,),
                    )
                reverted.append(migration_id)
            return reverted


# The old names for the rehearsal row and its key, kept so code written
# against 2.19 and earlier still imports. Deprecated since 2.20.0, removed
# in 3.0.
_RENAMED = {
    "receipt_key": "rehearsal_key",
    "RECEIPT_PASSED": "REHEARSAL_PASSED",
    "RECEIPT_FAILED": "REHEARSAL_FAILED",
    "RECEIPT_OVERRIDE": "REHEARSAL_OVERRIDE",
}


def __getattr__(name: str) -> Any:
    """The renamed names, with a warning naming what to import instead."""
    current = _RENAMED.get(name)
    if current is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    warnings.warn(
        f"sustained.migrations.{name} is deprecated and will be removed "
        f"in 3.0. Import {current} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return globals()[current]
