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
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional, Tuple, Type

from sustained.execution import notify_statement
from sustained.types import RelationType

if TYPE_CHECKING:
    from sustained.builder import QueryBuilder
    from sustained.model import Model


class AsyncAdapter:
    """
    The interface async execution needs from a driver. Subclasses implement
    fetch, execute, executemany, commit, and rollback.
    """

    async def fetch(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[List[str], List[Any]]:
        """Runs a statement and returns (column names, rows)."""
        raise NotImplementedError

    async def execute(self, sql: str, params: Tuple[Any, ...]) -> int:
        """Runs a statement and returns the affected row count."""
        raise NotImplementedError

    async def executemany(self, sql: str, seq_of_params: List[Tuple[Any, ...]]) -> int:
        """Runs a statement for every parameter tuple."""
        raise NotImplementedError

    async def commit(self) -> None:
        raise NotImplementedError

    async def rollback(self) -> None:
        raise NotImplementedError


class DbApiAsyncAdapter(AsyncAdapter):
    """
    Adapts a synchronous DB-API 2.0 connection to the async interface by
    running each call in a worker thread. The connection must allow use
    from other threads, e.g. sqlite3.connect(..., check_same_thread=False).
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        # One statement at a time per connection; DB-API connections are
        # not safe for concurrent use.
        self._lock = asyncio.Lock()

    def _fetch_sync(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[List[str], List[Any]]:
        cursor = self._connection.cursor()
        cursor.execute(sql, params)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return columns, cursor.fetchall()

    def _execute_sync(self, sql: str, params: Tuple[Any, ...]) -> int:
        cursor = self._connection.cursor()
        cursor.execute(sql, params)
        return int(cursor.rowcount)

    def _executemany_sync(self, sql: str, seq: List[Tuple[Any, ...]]) -> int:
        cursor = self._connection.cursor()
        cursor.executemany(sql, seq)
        return int(cursor.rowcount)

    async def fetch(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[List[str], List[Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_sync, sql, params)

    async def execute(self, sql: str, params: Tuple[Any, ...]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, sql, params)

    async def executemany(self, sql: str, seq_of_params: List[Tuple[Any, ...]]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._executemany_sync, sql, seq_of_params)

    async def commit(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.commit)

    async def rollback(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.rollback)


class AiosqliteAdapter(AsyncAdapter):
    """Adapts an aiosqlite connection."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def fetch(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[List[str], List[Any]]:
        cursor = await self._connection.execute(sql, params)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = await cursor.fetchall()
        return columns, list(rows)

    async def execute(self, sql: str, params: Tuple[Any, ...]) -> int:
        cursor = await self._connection.execute(sql, params)
        return int(cursor.rowcount)

    async def executemany(self, sql: str, seq_of_params: List[Tuple[Any, ...]]) -> int:
        cursor = await self._connection.executemany(sql, seq_of_params)
        return int(cursor.rowcount)

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()


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

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def fetch(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Tuple[List[str], List[Any]]:
        records = await self._connection.fetch(convert_format_to_numbered(sql), *params)
        if not records:
            return [], []
        columns = list(records[0].keys())
        return columns, [tuple(r) for r in records]

    async def execute(self, sql: str, params: Tuple[Any, ...]) -> int:
        status = await self._connection.execute(
            convert_format_to_numbered(sql), *params
        )
        # asyncpg returns a status string such as 'INSERT 0 3' or 'DELETE 2'.
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (ValueError, AttributeError):
            return -1

    async def executemany(self, sql: str, seq_of_params: List[Tuple[Any, ...]]) -> int:
        await self._connection.executemany(
            convert_format_to_numbered(sql), seq_of_params
        )
        # asyncpg's executemany reports no row count.
        return -1

    async def commit(self) -> None:
        # asyncpg runs in autocommit outside explicit transactions.
        pass

    async def rollback(self) -> None:
        pass


# Adapter pinned by an open async_transaction() block. A ContextVar keeps
# the pin scoped to the current task tree.
_pinned_adapter: ContextVar[Optional[AsyncAdapter]] = ContextVar(
    "sustained_pinned_adapter", default=None
)
# Adapters with an open transaction; arun() skips per-statement commits.
_active_async_transactions: Dict[int, AsyncAdapter] = {}


def resolve_adapter(
    explicit: Optional[AsyncAdapter], model_class: Type["Model"]
) -> AsyncAdapter:
    """Resolves the adapter: explicit, then pinned, then the model binding."""
    if explicit is not None:
        return explicit
    pinned = _pinned_adapter.get()
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
    return _active_async_transactions.get(id(adapter)) is adapter


@asynccontextmanager
async def async_transaction(adapter: AsyncAdapter) -> AsyncIterator[AsyncAdapter]:
    """
    Runs the block atomically on the adapter: commit on success, rollback
    on exception. The adapter pins to the current task context, so arun()
    calls inside the block use it without passing it around. Savepoint
    nesting is not supported; nested blocks raise.
    """
    if in_async_transaction(adapter):
        raise RuntimeError(
            "async_transaction() does not support nesting on one adapter."
        )
    key = id(adapter)
    _active_async_transactions[key] = adapter
    token = _pinned_adapter.set(adapter)
    try:
        # Explicit statements rather than adapter.commit(), because drivers
        # in autocommit mode (asyncpg) treat commit() as a no-op.
        await adapter.execute("BEGIN", ())
        try:
            yield adapter
        except BaseException:
            await adapter.execute("ROLLBACK", ())
            raise
        await adapter.execute("COMMIT", ())
    finally:
        _pinned_adapter.reset(token)
        del _active_async_transactions[key]


async def run_async(
    query: "QueryBuilder", adapter: Optional[AsyncAdapter] = None
) -> Any:
    """
    Executes a built query on an async adapter. SELECT statements return
    hydrated model instances with eager relations attached; writes return
    the affected row count or RETURNING rows as dicts.
    """
    resolved = resolve_adapter(adapter, query._model_class)

    use_executemany = (
        query._stmt_type == "insert"
        and len(query._insert_rows) > 1
        and not query._returning_columns
    )
    started = time.perf_counter()
    if query._stmt_type == "select":
        sql, params = query.to_sql()
        columns, rows = await resolved.fetch(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)
        models = [query._model_class(**dict(zip(columns, row))) for row in rows]
        for relation_name in query._eager_relations:
            await _eager_load_async(query._model_class, resolved, models, relation_name)
        return models

    if use_executemany:
        template = query.clone()
        template._insert_rows = [query._insert_rows[0]]
        sql, _ = template.to_sql()
        column_names = list(query._insert_rows[0].keys())
        seq = [tuple(row[c] for c in column_names) for row in query._insert_rows]
        result: Any = await resolved.executemany(sql, seq)
        notify_statement(sql, (), time.perf_counter() - started)
    elif query._returning_columns:
        sql, params = query.to_sql()
        columns, rows = await resolved.fetch(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)
        result = [dict(zip(columns, row)) for row in rows]
    else:
        sql, params = query.to_sql()
        result = await resolved.execute(sql, params)
        notify_statement(sql, params, time.perf_counter() - started)

    if not in_async_transaction(resolved):
        await resolved.commit()
    return result


async def _eager_load_async(
    model_class: Type["Model"],
    adapter: AsyncAdapter,
    parents: List["Model"],
    relation_name: str,
) -> None:
    """Async mirror of the sync eager loader for basic relations."""
    from sustained.execution import _collect_parent_keys, _split_column_ref
    from sustained.model import resolve_model_reference

    if not parents:
        return
    relation = model_class.relationMappings[relation_name]
    join_info = relation["join"]
    if "through" in join_info:
        raise NotImplementedError(
            "Async eager loading of through relations is not supported yet."
        )
    related_cls = resolve_model_reference(
        relation["modelClass"], context_module=model_class.__module__
    )
    from_table, from_col = _split_column_ref(join_info["from"], relation_name)
    to_table, to_col = _split_column_ref(join_info["to"], relation_name)
    if from_table == model_class.tableName:
        parent_col, child_col = from_col, to_col
    else:
        parent_col, child_col = to_col, from_col

    parent_keys = _collect_parent_keys(parents, parent_col, relation_name)
    unique_keys = [k for k in dict.fromkeys(parent_keys) if k is not None]
    is_many = relation["relation"] == RelationType.HasManyRelation
    if not unique_keys:
        for parent in parents:
            setattr(parent, relation_name, [] if is_many else None)
        return

    children = await run_async(
        related_cls.query().whereIn(child_col, unique_keys), adapter
    )
    grouped: Dict[Any, List["Model"]] = {}
    for child in children:
        grouped.setdefault(child.__dict__.get(child_col), []).append(child)
    for parent, key in zip(parents, parent_keys):
        matches = grouped.get(key, [])
        if is_many:
            setattr(parent, relation_name, matches)
        else:
            setattr(parent, relation_name, matches[0] if matches else None)
