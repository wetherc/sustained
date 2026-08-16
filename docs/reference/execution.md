---
layout: default
title: Execution and pooling reference
---

`sustained.execution`, `sustained.pool`, `sustained.aio`, and
`sustained.rendering`. The machinery between a finished query and a database.

Guide: [Executing Queries](/executing).

## Connection resolution

Every execution method resolves a connection in the same order:

1. The `connection` argument, when given.
2. A connection pinned to this thread by an open `transaction()` block.
3. The binding from `Model.bind()`.

With none of the three, `RuntimeError`. A `ConnectionPool` found at step 1 or
3 is checked out for the duration of the statement and released after.

## Transactions

In `sustained.execution`. Reach these through `Model.transaction()` rather
than directly.

| Signature | Description |
| --- | --- |
| `transaction(connection)` | A context manager. Commits when the block finishes, rolls back on any exception. |
| `in_transaction(connection)` | Whether a transaction is open on this connection. |
| `connection_scope(explicit, binding)` | The resolution above, as a context manager. |

Nested blocks on one connection use savepoints named `sustained_sp_<depth>`,
so an inner failure rolls back only the inner block. Inside a transaction,
`run()` stops committing per statement.

With a pool, the first block checks a connection out and pins it to the
thread; every statement in the block uses that one connection, and nested
blocks reuse it as savepoints.

## Statement logging

```python
set_statement_listener(listener) -> None
```

Registers a callable invoked after every executed statement with the SQL text,
the parameter tuple, and the duration in seconds. Pass `None` to remove it.

The listener is global. It is not per model, per connection, or per thread.

```python
from sustained.execution import set_statement_listener

set_statement_listener(lambda sql, params, seconds: log.info('%s %r %.3fs', sql, params, seconds))
```

## Hydration and eager loading

| Signature | Returns | Description |
| --- | --- | --- |
| `fetch_models(model_class, cursor)` | `list[Model]` | Builds instances from a cursor. Returns `[]` when the cursor has no description. |
| `eager_load_relation(model_class, connection, parents, relation_name)` | `None` | Runs one query and attaches the results to the parent instances. |

`eager_load_relation` raises `ValueError` for an unknown relation name, a join
reference that is not `table.column`, or rows missing the join column. A
`HasManyRelation` attaches a list; the to-one types attach a single instance
or `None`.

## `ConnectionPool`

In `sustained.pool`.

```python
ConnectionPool(factory, max_size=5, timeout=30.0)
```

Creates connections lazily from `factory`, up to `max_size`, and reuses
released ones. Bind it the way you would a connection. `max_size` below 1
raises `ValueError`.

| Member | Returns | Description |
| --- | --- | --- |
| `size` | `int` | Connections created so far. |
| `connection()` | context manager | Checks one out and guarantees release. |
| `acquire_raw()` | connection | Checks one out. You must release it. |
| `release(connection)` | `None` | Returns it to the pool, or closes it when the pool is closed. |
| `close()` | `None` | Closes idle connections. Checked-out ones close on release. |

`acquire_raw()` raises `PoolTimeout` when the pool stays exhausted past
`timeout`, and `RuntimeError` when the pool is closed. A factory that raises
does not consume a slot.

```python
from sustained.pool import ConnectionPool

pool = ConnectionPool(lambda: psycopg.connect(DSN), max_size=10)
Model.bind(pool)
```

## Async adapters

In `sustained.aio`. All share the same five methods, so a query does not know
which driver is underneath.

| Method | Returns |
| --- | --- |
| `await fetch(sql, params)` | `(column_names, rows)` |
| `await execute(sql, params)` | affected row count |
| `await executemany(sql, seq_of_params)` | affected row count |
| `await commit()` | `None` |
| `await rollback()` | `None` |

| Adapter | Wraps | Notes |
| --- | --- | --- |
| `DbApiAsyncAdapter(connection)` | Any synchronous DB-API connection | Runs each call in a worker thread under a lock. The connection must permit cross-thread use, so `sqlite3.connect(..., check_same_thread=False)`. |
| `AiosqliteAdapter(connection)` | aiosqlite | Awaits directly. |
| `AsyncpgAdapter(connection)` | asyncpg | Converts `%s` placeholders to `$1..$n`. asyncpg is autocommit, so `commit()` and `rollback()` do nothing, and `executemany()` returns `-1`. |

`AsyncAdapter` is the abstract base; subclass it for a driver not listed here.

### Async helpers

| Signature | Description |
| --- | --- |
| `async_transaction(adapter)` | An async context manager issuing `BEGIN`, `COMMIT`, and `ROLLBACK`. Raises `RuntimeError` on nesting: there are no savepoints. |
| `in_async_transaction(adapter)` | Whether one is open. |
| `resolve_adapter(explicit, model_class)` | Resolves argument, then the open transaction, then `Model.bind_async()`. Raises `RuntimeError` with none. |
| `await run_async(query, adapter=None)` | What `arun()` calls. |
| `convert_format_to_numbered(sql)` | Rewrites `%s` to `$1..$n`. |

The transaction pin lives in a `ContextVar`, so it follows the task tree
rather than the thread.

Two gaps: async transactions do not nest, and async eager loading of `through`
relations raises `NotImplementedError`.

## Rendering

`sustained.rendering`. Internal, but visible when a custom expression renders
itself.

`RenderContext(compiler, parameterize=False)` carries the compiler and the
value-handling mode. `ctx.value(v)` returns a placeholder and collects the
value when `parameterize` is set, and a formatted SQL literal when it is not.
An `Expression` renders verbatim either way. This is why `str(query)` and
`to_sql()` share one code path.

`bind_raw(sql, params, ctx)` fills `?` markers in a raw fragment, and raises
`ValueError` when the marker count does not match the parameter count.
