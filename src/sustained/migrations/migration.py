"""
The migration model: steps, the Migration class, and checksums.

A step is a SQL string, a ddl step, a list of either, or a callable that
receives the connection. This module renders steps, derives a down step
from a ddl up step, hashes the up statements for the tracking table, and
holds the helpers both migrators share to run a step and to mark the
errors a step raises.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Type,
    Union,
)

from sustained.ddl import DdlStep
from sustained.dialects import Dialects
from sustained.execution import cursor_scope
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.aio import AsyncAdapter
    from sustained.compilers.base import Compiler
    from sustained.model import Model

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
    and the error, which then propagates. before_migrate and
    after_migrate fire around up() only. on_error also fires when down()
    fails.

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
    hashed; validation then compares it like a computed one. A checksum on
    a step made of SQL raises ValueError: the statements hash themselves,
    and a stored checksum would hide an edit to them.

    A repeatable migration re-runs whenever its checksum changes, for
    views, functions, and seed data. Repeatables have no down step and
    run after every versioned migration.

    When the up step is a list of reversible ddl steps and no down is
    given, the down step derives itself: the inverses of the up steps,
    newest first. A ddl step that cannot reverse (a drop, add_enum_value,
    raw sql()) refuses the derivation; pass an explicit down step, or
    down=None to declare the migration irreversible. Repeatables never
    derive a down step.

    `transactional` says whether the migrator wraps the migration in a
    transaction. Set it to False for a statement the engine refuses
    inside a transaction block, such as CREATE INDEX CONCURRENTLY on
    Postgres. The flag covers the up step and the down step. A
    non-transactional migration that fails part way leaves the
    statements that already ran in the database; the migrator writes a
    failure row, so validation stops the next up() until you clean up
    and run repair().
    """

    def __init__(
        self,
        id: str,
        up: MigrationStep,
        down: Union[Optional[MigrationStep], _DeriveDown] = _DERIVE,
        checksum: Optional[str] = None,
        repeatable: bool = False,
        transactional: bool = True,
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
        if checksum is not None and _step_elements(up) is not None:
            raise ValueError(
                f"Migration '{id}' sets a checksum on a step made of SQL. "
                "An explicit checksum stands in for statements that cannot "
                "be hashed, which is a callable step. On SQL it replaces "
                "the hash of the statements and hides every later edit "
                "from validation. Drop the checksum."
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
        self.transactional = transactional


def migration_checksum(migration: Migration) -> Optional[str]:
    """
    The SHA-256 hex digest of a migration's up statements, each stripped of
    surrounding whitespace. Callable steps have no SQL to hash and return
    the migration's explicit checksum, or None when it has none. A ddl
    step hashes as its canonical signature rather than its rendered SQL,
    so the checksum stays the same on every dialect.

    Each statement enters the hash with its kind and its length in front
    of it. A statement list of ["A\nB"] and one of ["A", "B"] therefore
    hash differently, so splitting an applied SQL file in two reads as an
    edit.
    """
    if migration.checksum is not None:
        return migration.checksum
    elements = _step_elements(migration.up)
    if elements is None:
        return None
    digest = hashlib.sha256()
    for element in elements:
        if isinstance(element, DdlStep):
            kind, data = b"d", element.signature().encode("utf-8")
        else:
            kind, data = b"s", element.strip().encode("utf-8")
        digest.update(kind + len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _legacy_checksum(migration: Migration) -> Optional[str]:
    """
    The checksum releases before 2.25.0 stored: the same statements, each
    followed by a newline, with no length in front. A tracking row
    written by one of those releases stores this value, and it still
    matches its migration.
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


def _checksum_matches(stored: Optional[str], migration: Migration) -> bool:
    """
    True when a tracking row's checksum matches the migration as it
    stands: the current checksum, or for a row written before 2.25.0,
    the legacy one. A legacy row cannot tell a split statement from the
    original, which is the one edit it misses; repair() rewrites it in
    the current format.
    """
    if stored == migration_checksum(migration):
        return True
    return stored is not None and stored == _legacy_checksum(migration)


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


def _checked_steps(steps: int) -> int:
    """
    Raises when a revert count is below 0. A negative count would make
    `applied[-steps:]` a slice from the front of the list, which reverts
    almost every applied migration instead of the few the caller asked
    for. A count of 0 reverts nothing, which is what it has always done.
    """
    if steps < 0:
        raise ValueError(f"steps must be 0 or more, got {steps}.")
    return steps


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
