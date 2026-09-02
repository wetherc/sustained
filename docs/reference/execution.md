---
layout: default
title: Execution and pooling reference
---

`sustained.execution`, `sustained.pool`, `sustained.aio`, and `sustained.rendering`: everything between a finished query and the database.

Guide: [Executing Queries](/executing).

## Connection resolution

Every execution method resolves a connection in the same order:

1. The `connection` argument, when you pass one.
2. A connection pinned to this thread by an open `transaction()` block.
3. The binding from `Model.bind()`.

When none of the three resolves, the call raises `RuntimeError`. A `ConnectionPool` found at step 1 or step 3 is checked out for the length of the statement and released afterward.

## Connection types

These live in `sustained.types` and are re-exported from `sustained`.

| Name | What it is |
| --- | --- |
| `Connection` | Protocol: `cursor()`, `commit()`, `rollback()`, `close()`. |
| `Cursor` | Protocol: `execute()`, `executemany()`, `fetchone()`, `fetchall()`, `close()`, plus `description` and `rowcount`. |
| `Binding` | `Union[Connection, ConnectionPool]`, which is what `bind()` and every `connection=` argument take. |
| `SqlValue` | A value going into the database. Alias for `object`. |
| `RowValue` | A value read back out. Alias for `Any`. |

The two protocols list only the methods Sustained calls, so a driver connection matches by having those methods. No class subclasses the protocols, and no class registers with them.

## Transactions

These live in `sustained.execution`. Reach them through `Model.transaction()` rather than calling them yourself.

```python
transaction(connection, dialect=None)
```
{: .sig #transaction}

A context manager. Commits when the block finishes, and rolls back on any exception. The dialect chooses the savepoint spelling for nested blocks; `Model.transaction()` passes the model's dialect for you.

Statements inside the block share one cursor. That matters on DuckDB, whose driver autocommits every statement and gives every cursor its own session: the block opens, commits, and rolls back the transaction with SQL on that shared cursor.

```python
in_transaction(connection) -> bool
```
{: .sig #in_transaction}

Whether a transaction is open on this connection.

```python
connection_scope(explicit, binding)
```
{: .sig #connection_scope}

The resolution above, as a context manager.

Nested blocks on one connection use savepoints named `sustained_sp_<depth>`, so a failure in an inner block rolls back only that block. The statement follows the dialect: ANSI `SAVEPOINT` everywhere except MSSQL, which gets `SAVE TRANSACTION`. DuckDB has no savepoints, so a nested block raises `DialectError` before any statement runs. Inside a transaction, `run()` stops committing per statement.

With a pool, the first block checks a connection out and pins it to the thread. Every statement in the block uses that one connection, and nested blocks reuse it as savepoints.

## Statement logging

```python
set_statement_listener(listener)
```
{: .sig #set_statement_listener}

Registers a callable that runs after every executed statement, with the SQL text, the parameter tuple, and the duration in seconds. Pass `None` to remove the listener.

The listener is global: one listener covers every model, every connection, and every thread.

```python
from sustained.execution import set_statement_listener

set_statement_listener(lambda sql, params, seconds: log.info('%s %r %.3fs', sql, params, seconds))
```

## Hydration and eager loading

```python
fetch_models(model_class, cursor) -> list[Model]
```
{: .sig #fetch_models}

Builds instances from a cursor. Returns `[]` when the cursor has no description.

```python
eager_load_relation(model_class, connection, parents, relation_name)
```
{: .sig #eager_load_relation}

Runs one query and attaches the results to the parent instances. Raises `ValueError` for an unknown relation name, for a join reference that is not `table.column`, and for rows that are missing the join column. A `HasManyRelation` attaches a list, and the to-one relation types attach a single instance or `None`.

## `ConnectionPool`

`ConnectionPool` lives in `sustained.pool`.

```python
ConnectionPool(factory, max_size=5, timeout=30.0)
```
{: .sig}

The pool creates connections from `factory` as it needs them, up to `max_size`, and reuses released connections. Bind a pool the way you would bind a connection. A `max_size` below 1 raises `ValueError`.

```python
size -> int
```
{: .sig #size}

Property. The number of connections created so far.

```python
connection()
```
{: .sig #connection}

A context manager that checks a connection out and releases it at the end of the block.

```python
acquire_raw()
```
{: .sig #acquire_raw}

Checks a connection out. You have to release it yourself. Raises `PoolTimeout` when the pool stays exhausted past `timeout`, and `RuntimeError` when the pool is closed. A factory that raises does not consume a slot.

```python
release(connection)
```
{: .sig #release}

Returns the connection to the pool, or closes it when the pool is closed.

```python
close()
```
{: .sig #close}

Closes the idle connections. A checked-out connection closes on release.

```python
from sustained.pool import ConnectionPool

pool = ConnectionPool(lambda: psycopg.connect(DSN), max_size=10)
Model.bind(pool)
```

## Async adapters

These live in `sustained.aio`. Every adapter has the same methods, so a query does not need to know which driver is underneath.

| Method | Returns |
| --- | --- |
| `await fetch(sql, params)` | `(column_names, rows)` |
| `await execute(sql, params)` | affected row count, or `-1` |
| `await executemany(sql, seq_of_params)` | affected row count, or `-1` |
| `await commit()` | `None` |
| `await rollback()` | `None` |
| `await close()` | `None` |
| `async with scope()` | the adapter one call runs on |
| `driver_transaction_control()` | whether the driver opens the transaction |
| `await begin_where_ddl_autocommits()` | the `BEGIN` such a driver still needs |

A row count of `-1` means the driver reported none. Add `returning()` to the write when you need an exact count.

`driver_transaction_control()` tells `async_transaction()` how to open and close a block. It returns `False` on the base class and on `AsyncpgAdapter`, so the block runs `BEGIN`, `COMMIT`, and `ROLLBACK` as statements. `DbApiAsyncAdapter` returns `True`, because a DB-API 2.0 driver opens the transaction itself; the block then ends with `commit()` or `rollback()`. It returns `False` when the connection it wraps reports `autocommit` as `True`, because such a connection commits every statement as it runs and its `commit()` closes nothing. `begin_where_ddl_autocommits()` covers the one gap in that promise: sqlite3 in legacy transaction control leaves schema statements outside its implicit transaction, so `DbApiAsyncAdapter` sends a `BEGIN` there.

`scope()` is what every call opens before it runs. A plain adapter yields itself; a pool yields one of the adapters it holds and takes it back at the end, so a statement and its commit stay on one connection.

```python
DbApiAsyncAdapter(connection)
```
{: .sig #dbapiasyncadapter}

Wraps any synchronous DB-API connection. Runs each call in a worker thread under a lock. The connection has to permit cross-thread use, as in `sqlite3.connect(..., check_same_thread=False)`.

```python
AiosqliteAdapter(connection)
```
{: .sig #aiosqliteadapter}

Wraps aiosqlite and awaits the driver directly.

```python
AsyncpgAdapter(connection)
```
{: .sig #asyncpgadapter}

Wraps asyncpg. Converts `%s` placeholders to `$1..$n`. asyncpg is autocommit, so `commit()` and `rollback()` do nothing, `driver_transaction_control()` is `False`, and `executemany()` returns `-1`. `execute()` reads its count out of the status string and returns `-1` when the status holds none.

`AsyncAdapter` is the abstract base class. Subclass it for a driver that has no adapter here. `close()` does nothing on the base, for an adapter that borrows a connection it does not own.

## `AsyncConnectionPool`

`AsyncConnectionPool` lives in `sustained.aio_pool`.

```python
AsyncConnectionPool(factory, max_size=5, timeout=30.0)
```
{: .sig #asyncconnectionpool}

Pools adapters from an async `factory`, up to `max_size`, and reuses released ones. Bind it the way you bind an adapter. A `max_size` below 1 raises `ValueError`. One adapter runs one statement at a time, so a pool is how concurrent async queries reach the database in parallel.

The pool is an `AsyncAdapter` so it can be bound and passed like one, but it runs no statement itself: `fetch()`, `execute()`, `executemany()`, `commit()`, and `rollback()` all raise `RuntimeError`, because a write and its commit would land on two different connections.

```python
size -> int
```
{: .sig #async_size}

Property. The number of adapters opened so far.

```python
async with scope()
```
{: .sig #async_scope}

Checks an adapter out and releases it at the end of the block.

```python
await acquire()
```
{: .sig #async_acquire}

Checks an adapter out. You have to release it yourself. Raises `PoolTimeout` when the pool stays exhausted past `timeout`, and `RuntimeError` when the pool is closed.

```python
await release(adapter)
```
{: .sig #async_release}

Gives an adapter back, rolling it back first so a failed statement does not reach the next task. An adapter the pool did not hand out raises `ValueError`, which is what catches a double release. An adapter that cannot roll back is closed and dropped, and its slot reopens.

```python
await close()
```
{: .sig #async_close}

Closes the idle adapters and refuses new checkouts. A checked-out adapter closes on release.

```python
from sustained.aio import AsyncpgAdapter
from sustained.aio_pool import AsyncConnectionPool

async def open_adapter():
    return AsyncpgAdapter(await asyncpg.connect(DSN))

Model.bind_async(AsyncConnectionPool(open_adapter, max_size=10))
```

`AsyncMigrator` takes an adapter, not a pool: a migration run belongs on one session.

### Async helpers

```python
async_transaction(adapter, dialect=None)
```
{: .sig #async_transaction}

An async context manager that opens, commits, and rolls back the transaction. An adapter whose driver has no transaction control gets the dialect's own statements; the default dialect issues `BEGIN`, `COMMIT`, and `ROLLBACK`. An adapter over a DB-API 2.0 driver ends the block with `commit()` or `rollback()` instead, because that driver opened the transaction itself. Nested blocks on one adapter use savepoints named `sustained_sp_<depth>`, spelled per dialect; a dialect with no savepoints raises `DialectError` on nesting. A failed inner block rolls back to its savepoint and then releases it, so a later block can take the same name. If the rollback itself fails, the block's own error still propagates, with the rollback failure as its cause. `Model.async_transaction()` passes the model's dialect. Sustained tracks the nesting per adapter, so give each concurrent task its own adapter.

```python
in_async_transaction(adapter) -> bool
```
{: .sig #in_async_transaction}

Whether a transaction is open on this adapter.

```python
resolve_adapter(explicit, model_class)
```
{: .sig #resolve_adapter}

Resolves the argument, then the open transaction, then `Model.bind_async()`. Raises `RuntimeError` when none of the three resolves.

```python
await run_async(query, adapter=None)
```
{: .sig #run_async}

The function `arun()` calls.

```python
convert_format_to_numbered(sql) -> str
```
{: .sig #convert_format_to_numbered}

Rewrites `%s` markers to `$1..$n`.

The transaction pin lives in a `ContextVar`, so it follows the task tree rather than the thread.

Async eager loading shares the planner the synchronous path uses, so it covers dotted paths and `through` relations.

## Rendering

`sustained.rendering` is internal, and visible when a custom expression renders itself.

`RenderContext(compiler, parameterize=False)` carries the compiler and the value-handling mode. `ctx.value(v)` returns a placeholder and collects the value when `parameterize` is set, and returns a formatted SQL literal when it is not. An `Expression` renders as written in either mode. Both `str(query)` and `to_sql()` run through this one code path.

`bind_raw(sql, params, ctx)` fills the `?` markers in a raw fragment, and raises `ValueError` when the marker count does not match the parameter count.
