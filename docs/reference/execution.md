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

| Signature | Description |
| --- | --- |
| `transaction(connection)` | A context manager. Commits when the block finishes, and rolls back on any exception. |
| `in_transaction(connection)` | Whether a transaction is open on this connection. |
| `connection_scope(explicit, binding)` | The resolution above, as a context manager. |

Nested blocks on one connection use savepoints named `sustained_sp_<depth>`, so a failure in an inner block rolls back only that block. Inside a transaction, `run()` stops committing per statement.

With a pool, the first block checks a connection out and pins it to the thread. Every statement in the block uses that one connection, and nested blocks reuse it as savepoints.

## Statement logging

```python
set_statement_listener(listener) -> None
```

Registers a callable that runs after every executed statement, with the SQL text, the parameter tuple, and the duration in seconds. Pass `None` to remove the listener.

The listener is global: one listener covers every model, every connection, and every thread.

```python
from sustained.execution import set_statement_listener

set_statement_listener(lambda sql, params, seconds: log.info('%s %r %.3fs', sql, params, seconds))
```

## Hydration and eager loading

| Signature | Returns | Description |
| --- | --- | --- |
| `fetch_models(model_class, cursor)` | `list[Model]` | Builds instances from a cursor. Returns `[]` when the cursor has no description. |
| `eager_load_relation(model_class, connection, parents, relation_name)` | `None` | Runs one query and attaches the results to the parent instances. |

`eager_load_relation` raises `ValueError` for an unknown relation name, for a join reference that is not `table.column`, and for rows that are missing the join column. A `HasManyRelation` attaches a list, and the to-one relation types attach a single instance or `None`.

## `ConnectionPool`

`ConnectionPool` lives in `sustained.pool`.

```python
ConnectionPool(factory, max_size=5, timeout=30.0)
```

The pool creates connections from `factory` as it needs them, up to `max_size`, and reuses released connections. Bind a pool the way you would bind a connection. A `max_size` below 1 raises `ValueError`.

| Member | Returns | Description |
| --- | --- | --- |
| `size` | `int` | The number of connections created so far. |
| `connection()` | context manager | Checks a connection out and releases it at the end of the block. |
| `acquire_raw()` | connection | Checks a connection out. You have to release it yourself. |
| `release(connection)` | `None` | Returns the connection to the pool, or closes it when the pool is closed. |
| `close()` | `None` | Closes the idle connections. A checked-out connection closes on release. |

`acquire_raw()` raises `PoolTimeout` when the pool stays exhausted past `timeout`, and `RuntimeError` when the pool is closed. A factory that raises does not consume a slot.

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
| `await execute(sql, params)` | affected row count |
| `await executemany(sql, seq_of_params)` | affected row count |
| `await commit()` | `None` |
| `await rollback()` | `None` |

| Adapter | Wraps | Notes |
| --- | --- | --- |
| `DbApiAsyncAdapter(connection)` | Any synchronous DB-API connection | Runs each call in a worker thread under a lock. The connection has to permit cross-thread use, as in `sqlite3.connect(..., check_same_thread=False)`. |
| `AiosqliteAdapter(connection)` | aiosqlite | Awaits the driver directly. |
| `AsyncpgAdapter(connection)` | asyncpg | Converts `%s` placeholders to `$1..$n`. asyncpg is autocommit, so `commit()` and `rollback()` do nothing, and `executemany()` returns `-1`. |

`AsyncAdapter` is the abstract base class. Subclass it for a driver that has no adapter here.

### Async helpers

| Signature | Description |
| --- | --- |
| `async_transaction(adapter)` | An async context manager that issues `BEGIN`, `COMMIT`, and `ROLLBACK`. Nested blocks on one adapter use `SAVEPOINT sustained_sp_<depth>`. Sustained tracks the nesting per adapter, so give each concurrent task its own adapter. |
| `in_async_transaction(adapter)` | Whether a transaction is open on this adapter. |
| `resolve_adapter(explicit, model_class)` | Resolves the argument, then the open transaction, then `Model.bind_async()`. Raises `RuntimeError` when none of the three resolves. |
| `await run_async(query, adapter=None)` | The function `arun()` calls. |
| `convert_format_to_numbered(sql)` | Rewrites `%s` markers to `$1..$n`. |

The transaction pin lives in a `ContextVar`, so it follows the task tree rather than the thread.

Async eager loading shares the planner the synchronous path uses, so it covers dotted paths and `through` relations.

## Rendering

`sustained.rendering` is internal, and visible when a custom expression renders itself.

`RenderContext(compiler, parameterize=False)` carries the compiler and the value-handling mode. `ctx.value(v)` returns a placeholder and collects the value when `parameterize` is set, and returns a formatted SQL literal when it is not. An `Expression` renders as written in either mode. Both `str(query)` and `to_sql()` run through this one code path.

`bind_raw(sql, params, ctx)` fills the `?` markers in a raw fragment, and raises `ValueError` when the marker count does not match the parameter count.
