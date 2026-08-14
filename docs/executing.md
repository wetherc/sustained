---
layout: default
title: Executing Queries
---

Sustained can execute the queries it builds. It works with any DB-API 2.0 connection, such as `sqlite3`, `psycopg`, or `pyodbc`. Every statement runs parameterized: user values travel as parameters, never as text inside the SQL.

The connection's parameter style must match the dialect. The default and MSSQL dialects use `?` (qmark). The Postgres dialect uses `%s` (format).

## Binding a Connection

Bind a connection once with `Model.bind()`. Every query on that model can then run without passing the connection each time.

```python
import sqlite3
from sustained import Model

class User(Model):
    tableName = 'users'

conn = sqlite3.connect('app.db')
User.bind(conn)
```

Bind on `Model` itself to share one connection across all models. Bind on a subclass to scope it. Call `Model.unbind()` to remove a binding. You can also pass a connection directly to `run()` or `first()`, which overrides any binding.

## Running SELECT Queries

`run()` executes the query and hydrates each row into a model instance. Column names come from the cursor description.

```python
users = User.query().where('active', '=', True).orderBy('name').run()

for user in users:
    print(user.name)
```

`first()` runs the query with `LIMIT 1` and returns one instance, or `None` when there is no match. The original query is not changed.

```python
user = User.query().where('email', '=', 'ada@example.com').first()
```

## Writing Data

`insert()`, `update()`, and `delete()` turn the builder into a write statement. They use the same `where()` methods as SELECT and the same parameterized rendering.

```python
# INSERT INTO users (name, email) VALUES (?, ?)
User.query().insert({'name': 'Ada', 'email': 'ada@example.com'}).run()

# Multi-row insert. All rows must have the same columns.
User.query().insert([
    {'name': 'Ada'},
    {'name': 'Grace'},
]).run()

# UPDATE users SET active = ? WHERE id = ?
User.query().update({'active': False}).where('id', '=', 1).run()

# DELETE FROM users WHERE active = ?
User.query().delete().where('active', '=', False).run()
```

Write statements commit after they run and return the affected row count. Multi-row inserts without a RETURNING clause execute through the driver's `executemany()` with a single-row template, which is the fast path for bulk loads.

### Transactions

`Model.transaction()` opens a context that commits when the block finishes and rolls back when it raises. Statements inside the block share one transaction; `run()` stops committing per statement. Nested blocks use savepoints, so an inner failure rolls back only the inner block.

```python
with User.transaction():
    Account.query().update({'balance': 0}).where('id', '=', 1).run()
    AuditLog.query().insert({'event': 'reset', 'account_id': 1}).run()
```

### Upserts

Chain `onConflict(columns)` after `insert()`, then choose `merge()` to update the existing row or `ignore()` to skip it. `merge()` updates every inserted column except the conflict columns, or an explicit list.

```python
User.query().insert({'email': 'a@x.com', 'name': 'Ada'}) \
    .onConflict('email').merge().run()
```

Postgres, SQLite, and DuckDB render `ON CONFLICT`; MSSQL renders a `MERGE` statement; Presto raises.

### INSERT ... SELECT and CREATE TABLE AS

`insert_from(columns, query)` inserts the result of another query. `create_table_as(name, temporary=False)` turns a SELECT into a CTAS statement. MSSQL raises for CTAS; use `SELECT INTO` through raw SQL there.

```python
inactive = User.query().select('id', 'name').where('active', '=', False)
Archive.query().insert_from(['id', 'name'], inactive).run()

User.query().select('id').where('active', '=', True).create_table_as('active_ids').run()
```

### Safety Rule for UPDATE and DELETE

An `update()` or `delete()` without a `where()` clause raises a `ValueError`, because an unfiltered write usually means a missing filter. To write every row on purpose, add an always-true raw predicate:

```python
User.query().update({'active': True}).where(QueryBuilder.raw('1'), '=', 1).run()
```

### RETURNING

`returning()` adds a `RETURNING` clause on dialects that support it. The statement then returns a list of dicts instead of a row count. MSSQL and Presto raise a `DialectError`.

```python
rows = User.query().insert({'name': 'Ada'}).returning('id').run()
# [{'id': 42}]
```

## Eager Loading Relations

`withGraphFetched()` loads relations from `relationMappings` when the query runs. Each relation costs one extra query. `HasManyRelation` attaches a list to each instance. The to-one relation types attach a single instance or `None`.

```python
owners = Owner.query().withGraphFetched('pets').run()

for owner in owners:
    for pet in owner.pets:
        print(owner.name, pet.name)
```

Eager loading needs the join key columns in both result sets, so keep them in your `select()` or select all columns. Through relations (`ManyToManyRelation`) load with one query that joins the related table to the through table.

## Result Formats

`run()` returns model instances. For other shapes:

*   **`to_dicts()`**: rows as plain dicts keyed by column name.
*   **`to_df()`**: a pandas DataFrame, keeping the query's column names even when empty. Requires pandas.
*   **`to_arrow()`**: a pyarrow Table. Requires pyarrow.

pandas and pyarrow are optional; the methods raise a clear error when the library is missing.

## Connection Pooling

`ConnectionPool` creates connections lazily from a factory up to `max_size` and reuses released ones. Bind it like a connection; every statement checks a connection out for its duration.

```python
from sustained.pool import ConnectionPool

pool = ConnectionPool(lambda: psycopg2.connect(DSN), max_size=10)
User.bind(pool)

users = User.query().where('active', '=', True).run()

with User.transaction():
    # One connection is pinned to this thread for the whole block.
    User.query().insert({...}).run()
    Account.query().update({...}).where(...).run()
```

An exhausted pool raises `PoolTimeout` after the configured timeout. `pool.close()` closes idle connections.

## Async Execution

Queries run asynchronously through an adapter. `DbApiAsyncAdapter` wraps any synchronous DB-API connection in a worker thread; `AiosqliteAdapter` and `AsyncpgAdapter` wrap their native drivers. The asyncpg adapter converts `%s` placeholders to `$1..$n`.

```python
from sustained.aio import DbApiAsyncAdapter

adapter = DbApiAsyncAdapter(sqlite3.connect('app.db', check_same_thread=False))
User.bind_async(adapter)

users = await User.query().where('active', '=', True).arun()
user = await User.query().where('id', '=', 1).afirst()
rows = await User.query().ato_dicts()

async with User.async_transaction():
    await User.query().insert({...}).arun()
    await Account.query().update({...}).where(...).arun()
```

`arun()` mirrors `run()`: hydration, RETURNING rows, batched multi-row inserts, and eager loading of basic relations. Async eager loading of through relations is not supported yet, and async transactions do not nest.

## Statement Logging

`sustained.execution.set_statement_listener(fn)` registers an observer called after every executed statement with the SQL text, the parameter tuple, and the duration in seconds. Pass `None` to remove it.
