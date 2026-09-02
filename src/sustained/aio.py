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
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from sustained.execution import checked_columns, notify_statement
from sustained.types import (
    ColumnDescription,
    Connection,
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


class AiosqliteConnection(Protocol):
    """What this module calls on an aiosqlite connection."""

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

    def _fetch_sync(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, params)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return columns, list(cursor.fetchall())

    def _execute_sync(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        with closing(self._connection.cursor()) as cursor:
            cursor.execute(sql, params)
            return int(cursor.rowcount)

    def _executemany_sync(self, sql: str, seq: List[Tuple[SqlValue, ...]]) -> int:
        with closing(self._connection.cursor()) as cursor:
            cursor.executemany(sql, seq)
            return int(cursor.rowcount)

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_sync, sql, params)

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, sql, params)

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._executemany_sync, sql, seq_of_params)

    async def commit(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.commit)

    async def rollback(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.rollback)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)

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

    _active_async_transactions[key] = (adapter, 0)
    token = _pinned_adapter.set(adapter)
    # The block is driven by the driver's own calls only when both the
    # dialect and the adapter have transaction control. DuckDB autocommits
    # every statement, and an adapter in autocommit mode (asyncpg) reads
    # commit() as a no-op, so those blocks run BEGIN, COMMIT and ROLLBACK
    # as statements. A DB-API driver opens its transaction itself, so a
    # BEGIN on top of it would report a transaction already in progress.
    driver_control = (
        compiler.driver_transaction_control() and adapter.driver_transaction_control()
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
        except BaseException:
            if driver_control:
                await adapter.rollback()
            else:
                rollback_sql = compiler.rollback_transaction_sql()
                if rollback_sql is not None:
                    await adapter.execute(rollback_sql, ())
            raise
        if driver_control:
            await adapter.commit()
        else:
            commit_sql = compiler.commit_transaction_sql()
            if commit_sql is not None:
                await adapter.execute(commit_sql, ())
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
        return await _run_query_on(query, resolved)


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
