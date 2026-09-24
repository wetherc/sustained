"""
Async execution support.

Queries run asynchronously through an adapter that wraps an async database
driver. Three adapters ship with Sustained:

- DbApiAsyncAdapter wraps any synchronous DB-API 2.0 connection and runs
  its calls in a worker thread. It works with every driver the sync path
  supports and is the reference implementation.
- AiosqliteAdapter wraps an aiosqlite connection.
- AsyncpgAdapter wraps an asyncpg connection and converts the Postgres
  compiler's %s placeholders to asyncpg's $1..$n style. A literal %s inside
  raw SQL text would be converted too; avoid it in raw fragments.

Bind an adapter with Model.bind_async(adapter), then use arun(), afirst(),
and ato_dicts() on queries. async_transaction() gives atomic blocks; the
pin travels through a ContextVar, so concurrent tasks do not share it.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from sustained.execution import checked_columns, enter_autocommit, notify_statement
from sustained.types import (
    ColumnDescription,
    Connection,
    Cursor,
    RelationTree,
    RowValue,
    SqlValue,
    WriteResult,
)

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.dialects import Dialects
    from sustained.model import Model
    from sustained.types import AnyQuery

_T = TypeVar("_T")


class AiosqliteConnection(Protocol):
    """
    What this module calls on an aiosqlite connection.

    `_conn` and `_execute` are aiosqlite internals: the sqlite3 connection
    and the call that runs a function on the thread that owns it. The
    public `isolation_level` property touches the sqlite3 connection from
    the event loop's thread, which sqlite3 refuses by default.
    """

    _conn: Connection

    async def _execute(self, fn: Callable[..., _T], /, *args: object) -> _T: ...

    async def execute(
        self, sql: str, parameters: Sequence[SqlValue] = ..., /
    ) -> "AiosqliteCursor": ...

    async def executemany(
        self, sql: str, parameters: Sequence[Sequence[SqlValue]], /
    ) -> "AiosqliteCursor": ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


class AiosqliteCursor(Protocol):
    """What this module reads off an aiosqlite cursor."""

    @property
    def description(self) -> Optional[Sequence[ColumnDescription]]: ...

    @property
    def rowcount(self) -> int: ...

    async def fetchall(self) -> Sequence[Sequence[RowValue]]: ...


class AsyncpgRecord(Protocol):
    """What this module reads off an asyncpg record."""

    def keys(self) -> Sequence[str]: ...

    def __iter__(self) -> "Iterator[RowValue]": ...


class AsyncpgConnection(Protocol):
    """What this module calls on an asyncpg connection."""

    async def fetch(self, sql: str, *args: SqlValue) -> Sequence[AsyncpgRecord]: ...

    async def execute(self, sql: str, *args: SqlValue) -> str: ...

    async def executemany(
        self, sql: str, args: Sequence[Sequence[SqlValue]]
    ) -> object: ...

    async def close(self) -> None: ...


class AsyncAdapter:
    """
    The interface async execution needs from a driver. Subclasses implement
    fetch, execute, executemany, commit, and rollback.
    """

    @asynccontextmanager
    async def scope(self) -> AsyncIterator["AsyncAdapter"]:
        """
        The adapter one call runs on, for the length of that call. A plain
        adapter is itself; a pool hands out one of the adapters it holds and
        takes it back at the end. Every statement of the call runs on what
        this yields, so a write and its commit stay on one connection.
        """
        yield self

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        """
        Keeps every statement inside the block on one database session.

        async_transaction() and a rehearsal run their BEGIN, their work and
        their COMMIT or ROLLBACK inside this block. The base does nothing,
        because aiosqlite and asyncpg run every statement on the connection
        itself, which is one session. An adapter that opens a new session
        per statement must override it: otherwise the BEGIN, the work and
        the ROLLBACK reach different sessions, and the work commits.
        """
        yield

    @asynccontextmanager
    async def autocommit_scope(self) -> AsyncIterator[None]:
        """
        Runs the block with the driver's own transaction control off, for
        a migration with transactional=False.

        The base runs the block as it is and commits after it, which suits
        a driver that runs in autocommit already, such as asyncpg. An
        adapter over a driver that opens transactions of its own, such as
        sqlite3, overrides it: SQLite ignores PRAGMA foreign_keys inside a
        transaction, so the pragma that ends a table rebuild would not
        turn the checks back on.
        """
        yield
        await self.commit()

    async def close(self) -> None:
        """
        Closes the connection behind the adapter. The base does nothing,
        for adapters that borrow a connection they do not own.
        """
        return None

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        """Runs a statement and returns (column names, rows)."""
        raise NotImplementedError

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        """
        Runs a statement and returns the affected row count, or -1 when the
        driver does not report one.
        """
        raise NotImplementedError

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        """
        Runs a statement for every parameter tuple and returns the total
        affected row count, or -1 when the driver does not report one.
        """
        raise NotImplementedError

    async def commit(self) -> None:
        raise NotImplementedError

    async def rollback(self) -> None:
        raise NotImplementedError

    def driver_transaction_control(self) -> bool:
        """
        Reports whether the driver opens a transaction on its own and ends
        it through commit() and rollback().

        The base returns False, because an adapter such as AsyncpgAdapter
        runs every statement in autocommit and reads commit() as a no-op.
        async_transaction() then drives the block with BEGIN, COMMIT and
        ROLLBACK statements instead. An adapter over a driver that follows
        DB-API 2.0 returns True, so the block does not send a BEGIN the
        driver already sent.
        """
        return False

    async def begin_where_ddl_autocommits(self) -> None:
        """
        Sends the BEGIN a driver with transaction control still needs.

        sqlite3 in legacy transaction control opens its implicit
        transaction before data statements only, so a schema statement
        would commit at once. The base does nothing.
        """
        return None


class DbApiAsyncAdapter(AsyncAdapter):
    """
    Adapts a synchronous DB-API 2.0 connection to the async interface by
    running each call in a worker thread. The connection must allow use
    from other threads, e.g. sqlite3.connect(..., check_same_thread=False).
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        # One statement at a time per connection; DB-API connections are
        # not safe for concurrent use.
        self._lock = asyncio.Lock()
        # The cursor an open session() block runs every statement on.
        self._session_cursor: Optional[Cursor] = None

    async def _call(self, fn: Callable[..., _T], *args: object) -> _T:
        """
        Runs one driver call in a worker thread under the adapter lock.

        A cancelled task cannot stop the thread. The lock stays held until
        the thread ends, and only then does the CancelledError propagate.
        Released at the cancellation, the lock would let the next call,
        such as a pool's rollback, run on the connection at the same time
        as the unfinished one.
        """
        async with self._lock:
            future = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # wait() never cancels the future it waits on, so a second
                # cancellation lands here and the wait starts again.
                while not future.done():
                    try:
                        await asyncio.wait({future})
                    except asyncio.CancelledError:
                        pass
                # The thread's own error is dropped for the cancellation.
                if not future.cancelled():
                    future.exception()
                raise

    @contextmanager
    def _cursor(self) -> Iterator[Cursor]:
        """
        The session's cursor inside a session() block, left open for the
        rest of the block. Outside one, a new cursor closed after use.
        """
        if self._session_cursor is not None:
            yield self._session_cursor
            return
        cursor = self._connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()

    def _fetch_sync(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return columns, list(cursor.fetchall())

    def _execute_sync(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            return int(cursor.rowcount)

    def _executemany_sync(self, sql: str, seq: List[Tuple[SqlValue, ...]]) -> int:
        with self._cursor() as cursor:
            cursor.executemany(sql, seq)
            return int(cursor.rowcount)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        # On DuckDB every cursor is its own session, so a cursor per
        # statement would put BEGIN, the work and ROLLBACK in different
        # sessions. The block opens one cursor and runs every statement on
        # it. A nested block keeps the outer block's cursor.
        if self._session_cursor is not None:
            yield
            return
        cursor = await self._call(self._connection.cursor)
        self._session_cursor = cursor
        try:
            yield
        finally:
            # The pin drops before the lock wait, so a cancellation during
            # that wait cannot leave later statements on a closed cursor.
            self._session_cursor = None
            await self._call(cursor.close)

    @asynccontextmanager
    async def autocommit_scope(self) -> AsyncIterator[None]:
        restore = await self._call(enter_autocommit, self._connection)
        try:
            yield
        finally:
            await self._call(restore)

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        return await self._call(self._fetch_sync, sql, params)

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        return await self._call(self._execute_sync, sql, params)

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        return await self._call(self._executemany_sync, sql, seq_of_params)

    async def commit(self) -> None:
        await self._call(self._connection.commit)

    async def rollback(self) -> None:
        await self._call(self._connection.rollback)

    async def close(self) -> None:
        await self._call(self._connection.close)

    def driver_transaction_control(self) -> bool:
        # A DB-API 2.0 connection opens its transaction itself and ends it
        # through commit() and rollback(). A connection the caller put in
        # autocommit does not: it commits every statement as it runs, and
        # commit() closes nothing. The block is then driven with BEGIN,
        # COMMIT and ROLLBACK statements, the way it is for an adapter
        # that runs in autocommit of its own.
        return getattr(self._connection, "autocommit", False) is not True

    async def begin_where_ddl_autocommits(self) -> None:
        from sustained.execution import needs_explicit_begin

        if needs_explicit_begin(self._connection):
            await self.execute("BEGIN", ())


class AiosqliteAdapter(AsyncAdapter):
    """Adapts an aiosqlite connection."""

    def __init__(self, connection: AiosqliteConnection) -> None:
        self._connection = connection

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        cursor = await self._connection.execute(sql, params)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()
        return columns, list(rows)

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        cursor = await self._connection.execute(sql, params)
        return int(cursor.rowcount)

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        cursor = await self._connection.executemany(sql, seq_of_params)
        return int(cursor.rowcount)

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()

    @asynccontextmanager
    async def autocommit_scope(self) -> AsyncIterator[None]:
        # The switch and its restore run on aiosqlite's own thread, which
        # owns the sqlite3 connection.
        connection = self._connection
        restore = await connection._execute(enter_autocommit, connection._conn)
        try:
            yield
        finally:
            await connection._execute(restore)

    async def close(self) -> None:
        await self._connection.close()


def convert_format_to_numbered(sql: str) -> str:
    """Converts %s placeholders to $1..$n for asyncpg."""
    pieces = sql.split("%s")
    out = [pieces[0]]
    for index, piece in enumerate(pieces[1:], start=1):
        out.append(f"${index}")
        out.append(piece)
    return "".join(out)


class AsyncpgAdapter(AsyncAdapter):
    """
    Adapts an asyncpg connection. Statements arrive with the Postgres
    compiler's %s placeholders and are converted to $1..$n.
    """

    def __init__(self, connection: AsyncpgConnection) -> None:
        self._connection = connection

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        records = await self._connection.fetch(convert_format_to_numbered(sql), *params)
        if not records:
            return [], []
        columns = list(records[0].keys())
        return columns, [tuple(r) for r in records]

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        status = await self._connection.execute(
            convert_format_to_numbered(sql), *params
        )
        # asyncpg returns a status string such as 'INSERT 0 3' or 'DELETE 2'.
        # A status with no number at the end gives -1, the unknown count.
        # Add returning() to the write when you need an exact count.
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return -1

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        await self._connection.executemany(
            convert_format_to_numbered(sql), seq_of_params
        )
        # asyncpg's executemany reports no row count, so the count is
        # unknown. Add returning() to the insert when you need an exact
        # one; the query then runs one statement and returns its rows.
        return -1

    async def commit(self) -> None:
        # asyncpg runs in autocommit outside explicit transactions.
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        await self._connection.close()


# Adapter pinned by an open async_transaction() block. A ContextVar keeps
# the pin scoped to the current task tree.
_pinned_adapter: ContextVar[Optional[AsyncAdapter]] = ContextVar(
    "sustained_pinned_adapter", default=None
)
# The adapter async_transaction() was called with, when that differs from
# the one it pinned: a pool. A statement handed the same pool inside the
# block must run on the adapter the block checked out, not on a second
# checkout, which would sit outside the transaction and deadlock a pool
# of one.
_pinned_source: ContextVar[Optional[AsyncAdapter]] = ContextVar(
    "sustained_pinned_source", default=None
)
# Adapters with an open transaction; arun() skips per-statement commits.
# The value holds a strong reference to the adapter, so the id cannot be
# reused while the entry exists, plus the current savepoint nesting depth.
_active_async_transactions: Dict[int, Tuple[AsyncAdapter, int]] = {}


def resolve_adapter(
    explicit: Optional[AsyncAdapter], model_class: Type["Model"]
) -> AsyncAdapter:
    """Resolves the adapter: explicit, then pinned, then the model binding."""
    pinned = _pinned_adapter.get()
    if explicit is not None:
        if pinned is not None and (
            explicit is pinned or explicit is _pinned_source.get()
        ):
            # The caller named the adapter or pool of an open block, so
            # the statement joins that transaction.
            return pinned
        return explicit
    if pinned is not None:
        return pinned
    bound = getattr(model_class, "_async_adapter", None)
    if bound is None:
        raise RuntimeError(
            "No async adapter. Bind one with Model.bind_async(adapter) "
            "or pass it to arun()."
        )
    return bound  # type: ignore[no-any-return]


def in_async_transaction(adapter: AsyncAdapter) -> bool:
    """Reports whether the adapter has an open async_transaction() block."""
    entry = _active_async_transactions.get(id(adapter))
    return entry is not None and entry[0] is adapter


@asynccontextmanager
async def async_transaction(
    adapter: AsyncAdapter, dialect: "Dialects | None" = None
) -> AsyncIterator[AsyncAdapter]:
    """
    Runs the block atomically on the adapter: commit on success, rollback
    on exception. The adapter pins to the current task context, so arun()
    calls inside the block use it without passing it around.

    A pool checks one adapter out for the whole block, so every statement
    in it shares the transaction, and gives it back at the end.

    Nested blocks on the same adapter use savepoints, spelled the way the
    dialect spells them; the default is the ANSI SAVEPOINT statement.
    Nesting raises DialectError on a dialect with no savepoints.
    Model.async_transaction() passes the model's dialect for you.

    Nesting is tracked per adapter, not per task. Two tasks that open a
    block on one adapter at the same time share one transaction, and the
    second one is read as nested. Give each concurrent task its own adapter,
    as a connection carries one transaction at a time in any case.

    The block is opened and closed the way the driver wants it. An adapter
    over a DB-API 2.0 driver opens its transaction itself, so the block
    ends with commit() or rollback() and sends no BEGIN. An adapter in
    autocommit mode, such as AsyncpgAdapter, gets BEGIN, COMMIT and
    ROLLBACK statements instead, as does any adapter on a dialect whose
    driver has no transaction control, such as DuckDB.
    """
    pinned = _pinned_adapter.get()
    if pinned is not None and (adapter is pinned or adapter is _pinned_source.get()):
        # The caller named the adapter or pool of an open block. Nesting
        # on the pinned adapter gives the block a savepoint there; a
        # second checkout from the pool would open an independent
        # transaction, and would deadlock against the outer block on a
        # pool of one adapter.
        async with _transaction_on(pinned, dialect) as active:
            yield active
        return
    async with adapter.scope() as pooled:
        source = _pinned_source.set(adapter if adapter is not pooled else None)
        try:
            async with _transaction_on(pooled, dialect) as active:
                yield active
        finally:
            _pinned_source.reset(source)


@asynccontextmanager
async def pinned_async_transaction(adapter: AsyncAdapter) -> AsyncIterator[None]:
    """
    Marks the adapter as inside a transaction the caller opens and ends
    itself, and pins it for the length of the block.

    async_transaction() decides the end of its block. A rehearsal decides
    for itself, because it rolls back only when every proof is collected.
    The block gets the rest of the machinery: arun() skips its commit,
    in_async_transaction() reports the adapter busy, and a nested
    async_transaction() takes a savepoint. Without it, a callable step
    that runs arun(adapter) would commit the rehearsed work.

    Raises:
        ValueError: If a transaction is already open on the adapter.
    """
    if in_async_transaction(adapter):
        raise ValueError("a transaction is already open on this adapter")
    key = id(adapter)
    _active_async_transactions[key] = (adapter, 0)
    token = _pinned_adapter.set(adapter)
    try:
        yield
    finally:
        _pinned_adapter.reset(token)
        del _active_async_transactions[key]


async def _undo_savepoint_async(
    compiler: "Compiler",
    adapter: AsyncAdapter,
    savepoint: str,
    error: BaseException,
) -> None:
    """
    Rolls a nested block back to its savepoint and drops the savepoint.

    A savepoint rolled back is still set, so every later block of the same
    name would stack on the connection. The block failed for a reason the
    caller cares about, so a failure here does not replace it: the original
    error keeps propagating with the rollback failure as its cause.
    """
    statements = [
        compiler.rollback_savepoint_sql(savepoint),
        compiler.release_savepoint_sql(savepoint),
    ]
    for statement in statements:
        if statement is None:
            continue
        try:
            await adapter.execute(statement, ())
        except Exception as rollback_error:
            raise error from rollback_error


@asynccontextmanager
async def _transaction_on(
    adapter: AsyncAdapter, dialect: "Dialects | None" = None
) -> AsyncIterator[AsyncAdapter]:
    """The transaction itself, on one adapter that is already checked out."""
    from sustained.dialects import Dialects

    if dialect is None:
        dialect = Dialects.DEFAULT
    compiler = Dialects.get_compiler(dialect)
    key = id(adapter)
    entry = _active_async_transactions.get(key)

    if entry is not None and entry[0] is adapter:
        from sustained.exceptions import DialectError

        depth = entry[1] + 1
        savepoint = f"sustained_sp_{depth}"
        set_sql = compiler.savepoint_sql(savepoint)
        if set_sql is None:
            raise DialectError(
                f"{dialect.name} has no savepoints, so a nested "
                "async_transaction() block cannot roll back on its own. Run "
                "the statements inside the outer block instead."
            )
        _active_async_transactions[key] = (adapter, depth)
        token = _pinned_adapter.set(adapter)
        try:
            await adapter.execute(set_sql, ())
            try:
                yield adapter
            except BaseException as error:
                await _undo_savepoint_async(compiler, adapter, savepoint, error)
                raise
            release_sql = compiler.release_savepoint_sql(savepoint)
            if release_sql is not None:
                await adapter.execute(release_sql, ())
        finally:
            _pinned_adapter.reset(token)
            _active_async_transactions[key] = (adapter, depth - 1)
        return

    async with adapter.session():
        _active_async_transactions[key] = (adapter, 0)
        token = _pinned_adapter.set(adapter)
        # The block is driven by the driver's own calls only when both the
        # dialect and the adapter have transaction control. DuckDB autocommits
        # every statement, and an adapter in autocommit mode (asyncpg) reads
        # commit() as a no-op, so those blocks run BEGIN, COMMIT and ROLLBACK
        # as statements. A DB-API driver opens its transaction itself, so a
        # BEGIN on top of it would report a transaction already in progress.
        driver_control = (
            compiler.driver_transaction_control()
            and adapter.driver_transaction_control()
        )
        try:
            if driver_control:
                await adapter.begin_where_ddl_autocommits()
            else:
                begin_sql = compiler.begin_transaction_sql()
                if begin_sql is not None:
                    await adapter.execute(begin_sql, ())
            try:
                yield adapter
                # The commit sits inside the try. A deferred constraint
                # that fails at COMMIT leaves the driver's transaction
                # open, and the next statement's commit would write the
                # failed block's rows.
                if driver_control:
                    await adapter.commit()
                else:
                    commit_sql = compiler.commit_transaction_sql()
                    if commit_sql is not None:
                        await adapter.execute(commit_sql, ())
            except BaseException:
                if driver_control:
                    await adapter.rollback()
                else:
                    rollback_sql = compiler.rollback_transaction_sql()
                    if rollback_sql is not None:
                        await adapter.execute(rollback_sql, ())
                raise
        finally:
            _pinned_adapter.reset(token)
            del _active_async_transactions[key]


async def run_async(
    query: "AnyQuery", adapter: Optional[AsyncAdapter] = None
) -> Union[List["Model"], WriteResult]:
    """
    Executes a built query on an async adapter. SELECT statements return
    hydrated model instances with eager relations attached; writes return
    the affected row count or RETURNING rows as dicts.

    A pool checks one adapter out for the whole call, so the statement, its
    eager loads, and its commit all reach the same connection.

    Raises:
        AmbiguousColumns: If the result set repeats a column name.
    """
    async with resolve_adapter(adapter, query._model_class).scope() as resolved:
        try:
            return await _run_query_on(query, resolved)
        except BaseException:
            # A write that raises outside a transaction would leave its
            # partial work pending, such as the rows an executemany sent
            # before the failing one, and the next write's commit would
            # keep them.
            if query._stmt_type != "select" and not in_async_transaction(resolved):
                await _rollback_quietly(resolved)
            raise


async def _rollback_quietly(adapter: AsyncAdapter) -> None:
    """
    Rolls back after a statement that failed, dropping a rollback error so
    the statement's own error is the one the caller sees.
    """
    try:
        await adapter.rollback()
    except Exception:
        pass


async def _run_query_on(
    query: "AnyQuery", resolved: AsyncAdapter
) -> Union[List["Model"], WriteResult]:
    """The query itself, on one adapter that is already checked out."""
    use_executemany = (
        query._stmt_type == "insert"
        and len(query._insert_rows) > 1
        and not query._returning_columns
        # An Expression renders as SQL text with no placeholder, so the row
        # would bind one value too many. Those inserts go through the
        # one-statement path, which renders every row.
        and not query._has_expression_values()
    )
    started = time.perf_counter()
    if query._stmt_type == "select":
        sql, params = query._compiler.prepare_execution(*query.to_sql())
        columns, rows = await resolved.fetch(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)
        names = checked_columns(columns)
        models = [query._model_class(**dict(zip(names, row))) for row in rows]
        await eager_load_paths_async(
            query._model_class, resolved, models, query._eager_relations
        )
        return models

    if use_executemany:
        template = query.clone()
        template._insert_rows = [query._insert_rows[0]]
        sql, _ = template.to_sql()
        column_names = list(query._insert_rows[0].keys())
        prepared = [
            query._compiler.prepare_execution(sql, tuple(row[c] for c in column_names))
            for row in query._insert_rows
        ]
        # A row whose preparation rewrote the statement, such as a None
        # parameter on Athena, cannot share the batch; those inserts run
        # one execute per row instead.
        if all(row_sql == sql for row_sql, _ in prepared):
            result: WriteResult = await resolved.executemany(
                sql, [values for _, values in prepared]
            )
        else:
            total = 0
            for row_sql, row_values in prepared:
                total += await resolved.execute(row_sql, row_values)
            result = total
        # The listener sees every row's values, flattened in the order they
        # were sent, so an audit of a batch insert holds the same
        # information as an audit of single-row inserts.
        notify_statement(
            sql,
            tuple(v for _, values in prepared for v in values),
            time.perf_counter() - started,
        )
    elif query._returning_columns:
        sql, params = query._compiler.prepare_execution(*query.to_sql())
        columns, rows = await resolved.fetch(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)
        returning_names = checked_columns(columns)
        result = [dict(zip(returning_names, row)) for row in rows]
    else:
        sql, params = query._compiler.prepare_execution(*query.to_sql())
        result = await resolved.execute(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)

    if not in_async_transaction(resolved):
        await resolved.commit()
    return result


async def eager_load_paths_async(
    model_class: Type["Model"],
    adapter: AsyncAdapter,
    parents: List["Model"],
    paths: List[str],
) -> None:
    """
    Loads every dotted relation path for a list of parent instances. Each
    relation costs one query per level, batched over all the parents at
    that level, exactly as the sync loader does.
    """
    from sustained.execution import relation_tree

    await _eager_load_tree_async(model_class, adapter, parents, relation_tree(paths))


async def _eager_load_tree_async(
    model_class: Type["Model"],
    adapter: AsyncAdapter,
    parents: List["Model"],
    tree: RelationTree,
) -> None:
    """Loads one level of the relation tree, then recurses into each child."""
    from sustained.execution import _attached_children, related_model

    for relation_name, children in tree.items():
        await _eager_load_async(model_class, adapter, parents, relation_name)
        if not children:
            continue
        next_parents = _attached_children(parents, relation_name)
        if next_parents:
            await _eager_load_tree_async(
                related_model(model_class, relation_name),
                adapter,
                next_parents,
                children,
            )


async def _eager_load_async(
    model_class: Type["Model"],
    adapter: AsyncAdapter,
    parents: List["Model"],
    relation_name: str,
) -> None:
    """
    Async mirror of the sync eager loader. It shares the sync planner, so
    both paths build the same query and group the rows the same way,
    including relations that run through a link table.
    """
    from sustained.execution import attach_eager_load, plan_eager_load

    if not parents:
        return
    plan = plan_eager_load(model_class, parents, relation_name)
    children = (
        cast(List["Model"], await run_async(plan.query, adapter))
        if plan.query is not None
        else []
    )
    attach_eager_load(plan, parents, children)
