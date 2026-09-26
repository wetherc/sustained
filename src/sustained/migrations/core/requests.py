"""
The requests the migrator core yields, one small class for each kind of
work only a driver can do.

A core generator never touches a connection. Where it needs the database,
a callback, or a block such as a transaction, it yields a request and
waits for the answer. Migrator answers on a blocking connection and
AsyncMigrator on an async adapter. A driver sends the result back into the
generator, or throws its own exception in at the yield, so the core's try,
except and finally blocks see a failed statement exactly where it ran,
and the exception that reaches the caller is still the driver's.

The requests fall into four groups.

Statements: Execute, ExecuteMany, Fetch, TakeLock, RunStep, Commit and
Rollback. ReadSchema and DiffSource read the live schema, the one for a
rehearsal's before-and-after comparison and the other for a diff against
the models.

Callbacks: Fire calls one of the migrator's callbacks with the connection
or adapter in front of its arguments, and awaits what it returns on the
async driver.

Guards: RefuseOpenTransaction and RefuseRehearsal raise when the caller
holds state only the driver can see, such as an open transaction() block.

Scopes: Transaction, Autocommit, Session and PinnedTransaction carry a
body, a core generator of their own. The driver opens the block, runs the
body to its end inside it, and sends back what the body returned. An
error the body raises leaves the block first, so the block rolls back or
restores what it opened, and is then thrown into the generator that
yielded the scope. BeginPinned belongs to PinnedTransaction.
"""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Generator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from sustained.migrations.migration import CallbackResult, MigrationStep
from sustained.types import SqlValue

T = TypeVar("T")


class Execute(NamedTuple):
    """
    One statement on a cursor of its own, answered with None.

    `params` None runs the statement bare: the driver is handed no
    parameters at all, so a DB-API driver leaves a percent sign in it
    alone. DDL, lock and probe statements run this way. A tuple binds the
    parameters after the dialect's prepare_execution().

    `pinned` runs the statement on the open transaction's cursor, where
    the blocking driver has one. A migration's own tracking write runs so,
    and so does the rehearsal's ROLLBACK: on DuckDB every fresh cursor is
    a session of its own, and a statement there would land outside the
    transaction it belongs to. The async driver has one session per block
    already and ignores the flag.
    """

    sql: str
    params: Optional[Tuple[SqlValue, ...]] = None
    pinned: bool = False


class ExecuteMany(NamedTuple):
    """
    Each statement once for every one of its parameter rows, in order and
    on one cursor, answered with None. The rows go to the driver's
    executemany() as they are, without prepare_execution().
    """

    batches: Sequence[Tuple[str, List[Tuple[SqlValue, ...]]]]


class Fetch(NamedTuple):
    """One query, answered with every row it returned. `params` as for Execute."""

    sql: str
    params: Optional[Tuple[SqlValue, ...]] = None


class TakeLock(NamedTuple):
    """
    One advisory lock statement, answered with the row it returned, or
    None when it returned none. The core reads the row, since MySQL and
    MSSQL report a refused lock there instead of raising.
    """

    statement: str


class RunStep(NamedTuple):
    """
    One migration step: its statements, or the callable, which the
    blocking driver hands the connection and the async driver the adapter,
    awaiting what it returns.
    """

    step: MigrationStep


class Commit(NamedTuple):
    """Commits the connection's transaction."""


class Rollback(NamedTuple):
    """
    Rolls the connection's transaction back. The driver's error comes
    back into the core like any other; rollback_quietly() drops it.
    """


class Fire(NamedTuple):
    """
    Calls a callback with the connection, or the adapter, and then `args`.
    The async driver awaits a result that is awaitable.
    """

    hook: Callable[..., CallbackResult]
    args: Tuple[object, ...] = ()


class ReadSchema(NamedTuple):
    """
    The live schema of the connection's own schema, answered with a
    Snapshot. A rehearsal reads one before its run and one after its down
    sweep.
    """


class DiffSource(NamedTuple):
    """
    What a diff against the models reads, answered with a blocking
    connection and the snapshot read through it, if any.

    The blocking driver answers with its own connection and no snapshot,
    so the diffing code reads the schema itself and can ask whether a
    table holds rows. The async driver reads the schema through its
    adapter, covering `schemas` on top of the connection's own, and
    answers with a connection that replays that recording.
    """

    schemas: Sequence[str]


class RefuseOpenTransaction(NamedTuple):
    """
    Raises when a transaction block is open on the connection: the run
    named by `verb` commits as it goes, and would take the caller's
    uncommitted work with it.
    """

    verb: str


class RefuseRehearsal(NamedTuple):
    """
    Raises when a rehearsal's rollback could not take its work back: on a
    connection in autocommit, or inside an open transaction block, whose
    work the rollback would take back too.
    """


class Transaction(NamedTuple):
    """
    Runs the body inside transaction() or async_transaction(): committed
    when it finishes, rolled back when it raises.
    """

    body: "Core[Any]"


class Autocommit(NamedTuple):
    """
    Runs the body with the driver's own transaction control off, for a
    migration with transactional=False, and commits after it where the
    driver runs in autocommit anyway.
    """

    body: "Core[Any]"


class Session(NamedTuple):
    """
    Keeps every statement of the body on one database session. The async
    driver enters its adapter's session(); a blocking connection is one
    session already.
    """

    body: "Core[Any]"


class PinnedTransaction(NamedTuple):
    """
    Runs the body inside a transaction the body ends itself, as a
    rehearsal does: registered, so a statement or callable step inside it
    joins it and a nested block takes a savepoint, but never committed or
    rolled back by the driver. The body sends BeginPinned first.
    """

    body: "Core[Any]"


class BeginPinned(NamedTuple):
    """
    Opens the pinned transaction in SQL where the driver has not already.
    pinned_transaction() sends its BEGIN on entry, so the blocking driver
    does nothing here; the async driver sends the dialect's BEGIN.
    """


Request = Union[
    Execute,
    ExecuteMany,
    Fetch,
    TakeLock,
    RunStep,
    Commit,
    Rollback,
    Fire,
    ReadSchema,
    DiffSource,
    RefuseOpenTransaction,
    RefuseRehearsal,
    Transaction,
    Autocommit,
    Session,
    PinnedTransaction,
    BeginPinned,
]

Core = Generator[Request, Any, T]
"""A piece of the migrator core: yields requests, returns a T."""

Scope = Union[Transaction, Autocommit, Session, PinnedTransaction]


def run_in(scope: Callable[["Core[Any]"], Scope], body: Core[T]) -> Core[T]:
    """Runs the body inside the scope and returns what the body returned."""
    result: T = yield scope(body)
    return result


def rollback_quietly() -> Core[None]:
    """
    Rolls back, dropping a refusal. It runs after a statement that failed,
    whose error is the one worth reporting.
    """
    try:
        yield Rollback()
    except Exception:
        pass
