---
layout: default
title: Executing Queries
---

To start running queries, you need to bind a model to a database connection:

```python
import sqlite3

Show.bind(sqlite3.connect('app.db'))

shows = (Show.query()
    .where('sold_out', '=', False)
    .orderBy('starts_at')
    .run()
)
# [Show(id=1, title='Nightcrawler', ...), ...]
```

Sustained works with any DB-API 2.0 connection and never opens one itself. It reads no connection strings and no config files: you build the connection and pass it in.

Every statement runs parameterized. Values travel as parameters and never as text inside the SQL, so `str(query)` and the string that reaches the database are deliberately different.

The examples use the venue booking schema from [Getting Started](./getting-started).

## Matching the driver to the dialect

The connection's parameter style has to match the dialect's placeholder. `to_sql()` renders the placeholder and `run()` hands the parameters straight to the driver, so a mismatch fails at execution time with a driver error rather than at build time:

| Dialect | Placeholder | Driver |
| --- | --- | --- |
| `DEFAULT`, `MSSQL`, `PRESTO`, `DUCKDB`, `ATHENA` | `?` | `sqlite3`, `pyodbc`, `trino`, `duckdb`, `pyathena` with `pyathena.paramstyle = "qmark"` |
| `POSTGRES`, `MYSQL` | `%s` | `psycopg`, `PyMySQL` |

[SQL Dialects](./dialects) provides more information about each supported driver, and gives sample connection patterns.

On Athena, `run()` also converts each parameter to the string form the Athena API wants before handing it to pyathena, and rewrites a `None` parameter's placeholder to a literal `NULL`. If you execute `to_sql()` output yourself there, pass it through `compiler.prepare_execution(sql, params)` first.

## Binding a connection

`Model.bind()` sets the connection as a class attribute, which subclasses inherit:

```python
from sustained import Model

Model.bind(conn)     # every model in the process
Show.bind(conn)      # only Show
Show.unbind()        # remove it again
```

A connection passed to `run()` or `first()` overrides any binding. This is useful when, e.g., one query needs to run against a replica while the rest use the primary.

Sustained looks for a connection in this order: the connection you passed to the call, then the model's own binding, then a binding inherited from a parent class. Running with none of those raises `RuntimeError`.

## Reading rows

`run()` executes the query and hydrates each row into a model instance, using the column names from the cursor description:

```python
for show in Show.query().where('venue_id', '=', 1).run():
    print(show.title, show.starts_at)
```

`first()` adds `LIMIT 1` and returns one instance, or `None` when nothing matches. It leaves the original query alone, so a builder you keep around is still safe to reuse:

```python
show = Show.query().where('title', '=', 'Nightcrawler').first()
```

### Other result data structures

`run()` gives you Model instances. If you want the same rows in another form, these methods are also available:

| Method | Returns |
| --- | --- |
| `to_dicts()` | plain dicts keyed by column name |
| `to_df()` | a pandas DataFrame, keeping the column names even when empty |
| `to_arrow()` | a pyarrow Table |

`pandas` and `pyarrow` are optional dependencies. The methods raise `RuntimeError` naming the install command when the library is missing.

### Type checking

The builder includes the underlying Model: `Show.query()` is a `QueryBuilder[Show]`, and the model survives every clause you chain, so `mypy` and Pyright read the results without a cast or an annotation:

```python
shows = (Show.query()
    .where('sold_out', '=', False)
    .orderBy('starts_at')
    .run()
)
# shows: List[Show]

show = Show.query().first()
# show: Optional[Show]

rows = Show.query().to_dicts()
# rows: List[Dict[str, Any]]
```

`insert()`, `insert_from()`, `create_table_as()`, `update()`, and `delete()` hand back a `WriteBuilder[Show]`, whose `run()` is the affected row count, or the RETURNING rows as dicts:

```python
removed = (Show.query()
    .delete()
    .where('starts_at', '<', cutoff)
    .run()
)
# removed: Union[int, List[Dict[str, Any]]]
```

`QueryBuilder` and `WriteBuilder` are one class at run time. The two names exist so that a type checker never reads a row count as a list of models. `isinstance(query, WriteBuilder)` is true for any builder, so do not test with it.

The columns are not typed. A `select()` does not narrow the model, and `to_dicts()` values stay `RowValue`, which is `Any`, because Python has no reasonable way to infer narrower types back out of the SQL string.

### Names for what you pass in

Sustained exports names for the untyped handoff points of the library. Import them from `sustained`:

```python
from sustained import Binding, Connection, Cursor, RowValue, SqlValue
```

`Connection` and `Cursor` describe the DB-API 2.0 methods Sustained calls. They are protocols, so a `sqlite3.Connection`, a `psycopg` connection, and a `pyodbc` connection all match without subclassing anything. Annotate a config module's factory with `Connection` and a type checker will expect a real driver:

```python
# sustained_config.py
import sqlite3

from sustained import Connection

def get_connection() -> Connection:
    return sqlite3.connect('app.db')
```

`Binding` is what `Model.bind()` takes and what every `connection=` argument accepts: one `Connection`, or a `ConnectionPool` that hands them out.

`SqlValue` and `RowValue` split values by direction. `SqlValue` is a value on its way into the database, bound as a parameter or rendered as a literal. It is `object`, not `Any`, so passing a query value where a column name belongs is still an error. `RowValue` is a value read back, and it stays `Any` because the driver decides whether a `NUMERIC` arrives as a `Decimal` or a `float`.

## Writing rows

`insert()`, `update()`, and `delete()` turn the builder into a write statement. They take the same `where()` methods and the same parameterized rendering as a SELECT:

```python
Show.query().insert({'venue_id': 1, 'title': 'Nightcrawler'}).run()
# INSERT INTO shows (venue_id, title) VALUES (?, ?)

Show.query().update({'sold_out': True}).where('id', '=', 1).run()
# UPDATE shows SET sold_out = ? WHERE id = ?

Ticket.query().delete().where('sold_at', 'IS', None).run()
# DELETE FROM tickets WHERE sold_at IS NULL
```

A write commits when it finishes, unless it is inside a transaction, and returns the affected row count.

The count is `-1` when the driver reports none. asyncpg does this for a batched multi-row insert, and for any statement whose status string ends without a number. Add `returning()` when you need an exact count: the write gives back one row per row it wrote, so `len()` of that list is the count.

A multi-row insert takes a list. Every row must have the same columns, so that the statement has one template:

```python
Ticket.query().insert([
    {'show_id': 1, 'price': 45},
    {'show_id': 1, 'price': 65},
]).run()
```

Without a RETURNING clause, a multi-row insert goes through the driver's `executemany()` with a single-row template, which is the fast path for bulk loads.

### UPDATE and DELETE need a WHERE

An `update()` or `delete()` with no `where()` raises `ValueError` before it reaches the database.

To write every row deliberately, use a predicate that is always true:

```python
from sustained.builder import QueryBuilder

(Show.query()
    .update({'sold_out': False})
    .where(QueryBuilder.raw('1'), '=', 1)
    .run()
)
```

### Upserts

Chain `onConflict(columns)` after `insert()`, then `merge()` to update the existing row or `ignore()` to leave it:

```python
(Artist.query()
    .insert({'name': 'Low', 'country': 'US'})
    .onConflict('name')
    .merge()
    .run()
)
```

`merge()` updates every inserted column except the conflict columns, or just the columns in an explicit list you pass it. The conflict columns need a unique constraint or primary key in the database, or the engine rejects the statement. A `merge()` with nothing left to update raises `ValueError`, which happens when every inserted column is also a conflict column.

Postgres, SQLite, and DuckDB render `ON CONFLICT`. MSSQL renders a `MERGE` statement. Presto raises `DialectError`.

### RETURNING

`returning()` adds the clause on dialects that have it. The statement then returns a list of dicts instead of a row count:

```python
rows = (Show.query()
    .insert({'venue_id': 1, 'title': 'Nightcrawler'})
    .returning('id')
    .run()
)
# [{'id': 42}]
```

MSSQL and Presto raise `DialectError`. On MSSQL, use `OUTPUT` through raw SQL instead.

### INSERT ... SELECT and CREATE TABLE AS

`insert_from(columns, query)` inserts another query's result. `create_table_as(name, temporary=False)` turns a SELECT into a CTAS statement:

```python
class ShowArchive(Model):
    tableName = 'show_archive'

past = Show.query().select('id', 'title').where('starts_at', '<', '2026-01-01')

ShowArchive.query().insert_from(['id', 'title'], past).run()
# INSERT INTO show_archive (id, title)
# SELECT id, title FROM shows WHERE starts_at < ?

(Show.query()
    .select('id')
    .where('sold_out', '=', True)
    .create_table_as('sellouts')
    .run()
)
# CREATE TABLE sellouts AS SELECT id FROM shows WHERE sold_out = ?
```

`insert_from()` writes to the table of the model it is called on. The target is the model in front of `.query()`, and the source is the query you pass in.

MSSQL raises `DialectError` for CTAS. Use `SELECT INTO` through raw SQL there.

## Transactions

`Model.transaction()` opens a block that commits at the end and rolls back on an exception. Statements inside share one transaction, and `run()` stops committing per statement:

```python
with Show.transaction():
    Show.query().update({'sold_out': True}).where('id', '=', 1).run()

    (Ticket.query()
        .delete()
        .where('show_id', '=', 1)
        .andWhere('sold_at', 'IS', None)
        .run()
    )
```

Nested blocks use savepoints, so a failure inside an inner block rolls back only that block and the outer transaction carries on. The savepoint statement follows the model's dialect: MSSQL gets `SAVE TRANSACTION`, everything else the ANSI `SAVEPOINT`. DuckDB has no savepoints, so a nested block raises `DialectError` there.

## Eager loading relations

`withGraphFetched()` loads the relations named in `relationMappings` when the query runs, at one extra query per relation:

```python
venues = Venue.query().withGraphFetched('shows').run()

for venue in venues:
    for show in venue.shows:
        print(venue.name, show.title)
```

A `HasManyRelation` or `ManyToManyRelation` attaches a list. The to-one relation types attach a single instance or `None`.

Eager loading matches rows on the join key, so both result sets need that column. Keep it in your `select()`, or select every column. A relation through a link table loads with one query that joins the link table to the far table.

A dotted path loads a relation of a relation:

```python
venues = Venue.query().withGraphFetched('shows.tickets').run()

for venue in venues:
    for show in venue.shows:
        print(show.title, len(show.tickets))
```

Each level costs one query, batched over every instance at that level, so three venues with twelve shows load their tickets with one query, not twelve. Paths that share a prefix load the prefix once, so `withGraphFetched('shows.tickets', 'shows.artists')` runs three queries after the first. An unknown segment raises `ValueError` when you build the query, naming the segment and the model it was read from.

A join flattens the related rows into the result and repeats the parent row once per child. Eager loading costs one extra query and returns each object separately; a join costs one query and gives you a wide, flattened result.

## Connection pooling

`ConnectionPool` creates connections from a factory, up to `max_size`, and reuses released ones. You bind it exactly like a connection:

```python
from sustained.pool import ConnectionPool

pool = ConnectionPool(lambda: psycopg.connect(DSN), max_size=10)
Show.bind(pool)

shows = Show.query().where('sold_out', '=', False).run()
```

Each statement checks a connection out for its duration and releases it after. A transaction pins one connection to the thread for the whole block, so the savepoints and the commit land on the same session:

```python
with Show.transaction():
    Show.query().update({'sold_out': True}).where('id', '=', 1).run()
    Ticket.query().insert({'show_id': 1, 'price': 45}).run()
```

A query given the pool by hand inside the block runs on the pinned connection too, so `query.run(pool)` stays in the transaction instead of checking a second connection out.

An exhausted pool raises `PoolTimeout` after the configured timeout rather than blocking forever. `pool.close()` closes the idle connections.

A transaction belongs to the thread that opened it. A second thread that calls `transaction()` on the same connection gets a `RuntimeError`, because a connection carries one transaction and the block would otherwise nest inside the first thread's. Give each thread its own connection, from a pool or from a second bind target.

## Async execution

Async queries run through an adapter, which is the async equivalent of a connection:

| Adapter | Wraps |
| --- | --- |
| `AsyncpgAdapter` | asyncpg, converting `%s` placeholders to `$1..$n` |
| `AiosqliteAdapter` | aiosqlite |
| `DbApiAsyncAdapter` | any synchronous DB-API connection, in a worker thread |

Bind one with `bind_async()`, then use the `a`-prefixed methods:

```python
from sustained.aio import DbApiAsyncAdapter

Show.bind_async(DbApiAsyncAdapter(sqlite3.connect('app.db', check_same_thread=False)))

shows = await Show.query().where('sold_out', '=', False).arun()
show = await Show.query().where('id', '=', 1).afirst()
rows = await Show.query().ato_dicts()

async with Show.async_transaction():
    await Show.query().update({'sold_out': True}).where('id', '=', 1).arun()
```

`arun()` mirrors `run()`: hydration, RETURNING rows, batched multi-row inserts, and eager loading. Both paths share the same loader, so dotted paths, per-level batching, and relations through a link table behave the same way. `async_transaction()` mirrors `transaction()` as well: nested blocks open a savepoint, so an inner failure rolls back only the inner block. A rolled-back savepoint is released after, so the same block can run again under the same name.

`async_transaction()` opens and closes the block the way the driver wants it. `DbApiAsyncAdapter` wraps a DB-API 2.0 driver, which opens the transaction itself, so the block sends no `BEGIN` and ends with the driver's `commit()` or `rollback()`. A connection the caller put in autocommit is the exception: it commits every statement as it runs, so its blocks get the statements instead. `AsyncpgAdapter` runs in autocommit and reads `commit()` as a no-op, so its blocks get `BEGIN`, `COMMIT`, and `ROLLBACK` as statements. A dialect whose driver has no transaction control, such as DuckDB, gets the statements too. A custom adapter says which it is by overriding `driver_transaction_control()`, which returns `False` on the base class.

### Async pooling

One adapter runs one statement at a time. A connection carries one transaction, `DbApiAsyncAdapter` serializes every call behind one lock, and asyncpg sends one statement per connection. Ten concurrent `arun()` calls on one adapter queue up behind each other.

`AsyncConnectionPool` is the async twin of `ConnectionPool`. It opens adapters from an async factory, up to `max_size`, and binds like one:

```python
from sustained.aio import AsyncpgAdapter
from sustained.aio_pool import AsyncConnectionPool

async def open_adapter():
    return AsyncpgAdapter(await asyncpg.connect(DSN))

Show.bind_async(AsyncConnectionPool(open_adapter, max_size=10))

shows, tickets = await asyncio.gather(
    Show.query().arun(),
    Ticket.query().arun(),
)
```

Each call checks one adapter out for its whole length, the statement, its eager loads, and its commit alike, then gives it back. An `async_transaction()` block keeps one adapter from BEGIN to COMMIT. A released adapter is rolled back first, so a failed statement never reaches the next task. An exhausted pool raises `PoolTimeout`, the same error the blocking pool raises, and `await pool.close()` closes the idle adapters.

The pool runs no statement itself. `await pool.fetch(...)` raises, because a write and its commit would land on two different connections; take an adapter out with `async with pool.scope() as adapter` when you want to run something by hand. `AsyncMigrator` takes an adapter, not a pool: a migration run belongs on one session.

## Watching what runs

`set_statement_listener()` registers a function called after every executed statement, with the SQL text, the parameter tuple, and the duration in seconds:

```python
from sustained.execution import set_statement_listener

def log(sql, params, seconds):
    if seconds > 0.1:
        print(f'{seconds:.3f}s  {sql}')

set_statement_listener(log)
set_statement_listener(None)   # remove it
```

One listener is registered at a time, and it sees every statement in the process, including the migrator's.

## Where to go next

| You want to | Read |
| --- | --- |
| Pick a driver for your engine | [SQL Dialects](./dialects) |
| Create the tables you are querying | [Schema and Migrations](./schema) |
| Find the code for a specific task | [Recipes](./recipes) |
| See every method and what it raises | [Execution reference](./reference/execution) |
