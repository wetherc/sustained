---
layout: default
title: SQL Dialects
---

Sustained compiles the same query for every database engine it supports. You set the dialect once per model, usually at application startup, and every query, DDL statement, and migration for that model renders in that engine's SQL:

```python
from sustained.dialects import Dialects

User.set_dialect(Dialects.POSTGRES)
```

If an engine lacks a feature, the query raises `DialectError` when it builds, before anything reaches the database.

## Dialects, drivers, and placeholders

To execute queries, bind a DB-API 2.0 connection whose parameter style matches the dialect's placeholder. `to_sql()` renders that placeholder, and `run()` passes the parameters straight to the driver, so a mismatch fails at execution time.

| Dialect | Engine | Recommended driver | Placeholder | Quoting |
| --- | --- | --- | --- | --- |
| `Dialects.DEFAULT` | ANSI SQL, SQLite | `sqlite3` (standard library) | `?` | none |
| `Dialects.POSTGRES` | PostgreSQL | `psycopg` or `psycopg2` | `%s` | `"name"` |
| `Dialects.MSSQL` | Microsoft SQL Server | `pyodbc` | `?` | `[name]` |
| `Dialects.MYSQL` | MySQL, MariaDB | `PyMySQL` | `%s` | `` `name` `` |
| `Dialects.PRESTO` | Presto, Trino | `trino` or `presto-python-client` | `?` | `"name"` |
| `Dialects.ATHENA` | AWS Athena | `pyathena` | `%s` | `"name"` |
| `Dialects.DUCKDB` | DuckDB | `duckdb` | `?` | `"name"` |

Async execution wraps a driver in an adapter instead: `AsyncpgAdapter` for asyncpg, `AiosqliteAdapter` for aiosqlite, and `DbApiAsyncAdapter` for any synchronous driver in the table. See [Executing Queries](./executing#async-execution).

## Default (ANSI, SQLite)

The default dialect renders plain ANSI SQL with unquoted identifiers and `?` placeholders. SQLite's built-in driver matches it exactly, and the migration system treats it as SQLite: introspection reads the PRAGMA tables, and column changes rebuild the table, because SQLite cannot alter columns in place.

```python
import sqlite3
from sustained import Model

class User(Model):
    tableName = 'users'

User.bind(sqlite3.connect('app.db'))
users = User.query().where('active', '=', True).run()
```

Upserts render `ON CONFLICT`, RETURNING works, and `whereILike()` compiles to `LOWER(col) LIKE LOWER(pattern)`.

## PostgreSQL

Postgres supports the largest set of features: native `ILIKE`, `DISTINCT ON`, `RETURNING`, `ON CONFLICT` upserts, `for_update()` row locking, identity columns for `autoincrement`, `JSONB` for the `Json` type, and in-place `ALTER COLUMN` migrations with `USING` cast hints. Migration runs hold a `pg_advisory_lock`, so concurrent deploys queue. Placeholders are `%s`, matching psycopg.

```python
import psycopg
from sustained.dialects import Dialects

User.set_dialect(Dialects.POSTGRES)
User.bind(psycopg.connect('dbname=app user=app'))

row = (User.query()
    .insert({'name': 'Ada'})
    .returning('id')
    .run()
)
```

For connection pooling, pass the factory to `ConnectionPool`:

```python
from sustained.pool import ConnectionPool

User.bind(ConnectionPool(lambda: psycopg.connect(DSN), max_size=10))
```

## Microsoft SQL Server

MSSQL quotes identifiers with brackets and uses `?` placeholders, matching pyodbc. Booleans render as `1`/`0`, `Boolean` columns as `BIT`, strings as `NVARCHAR`, and timestamps as `DATETIME2`. `top(n)` renders `TOP n`. `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL only allows after `orderBy()`. Upserts render a `MERGE` statement. `NOW()` translates to `GETDATE()` and `LENGTH()` to `LEN()`.

```python
import pyodbc
from sustained.dialects import Dialects

User.set_dialect(Dialects.MSSQL)
User.bind(pyodbc.connect('DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=app'))

newest = (User.query()
    .orderBy('created_at', 'desc')
    .limit(10)
    .run()
)
```

RETURNING, CTAS, and `explain()` raise `DialectError`. Use `OUTPUT`, `SELECT INTO`, and SSMS plans through raw SQL instead. Migrations rename with `sp_rename`, alter columns by restating the full definition, and hold an `sp_getapplock` session lock while they run.

## MySQL and MariaDB

The `MYSQL` dialect serves both MySQL and MariaDB; Sustained does not distinguish between the two. Identifiers quote with backticks and placeholders are `%s`, matching PyMySQL, mysqlclient, and mysql-connector. Upserts render `ON DUPLICATE KEY UPDATE`. `for_update()` works, with `SKIP LOCKED` and `NOWAIT` on MySQL 8.0 and later. Migration runs hold a `GET_LOCK` session lock.

```python
import pymysql
from sustained.dialects import Dialects

User.set_dialect(Dialects.MYSQL)
User.bind(pymysql.connect(host='db.internal', user='app', database='app'))

newest = (User.query()
    .orderBy('created_at', 'desc')
    .limit(10)
    .run()
)
```

Column types render in the spelling `information_schema` reports back, so a column never drifts against the DDL that created it:

| Sustained | MySQL |
| --- | --- |
| `Integer()` | `INT` |
| `BigInteger()` | `BIGINT` |
| `String(120)` | `VARCHAR(120)` |
| `Text()` | `TEXT` |
| `Boolean()` | `TINYINT(1)` |
| `Float()` | `DOUBLE` |
| `Numeric(18, 6)` | `DECIMAL(18, 6)` |
| `Date()` | `DATE` |
| `Timestamp()` | `DATETIME` |
| `Json()` | `JSON` |

`Timestamp()` maps to `DATETIME` rather than `TIMESTAMP`. MySQL's `TIMESTAMP` is four bytes, stops in 2038, and converts time zones on the way in and out, which is not what `Timestamp()` describes.

RETURNING raises `DialectError`, even though MariaDB, which supports it: since Sustained shares one dialect for both, it needs to take a more conservative approach. Read the row back with a second query, or use `LAST_INSERT_ID()` through raw SQL. `STRING_AGG` raises as well, rather than translating to `GROUP_CONCAT`, whose separator is a keyword and not a second argument. A whole `Text()`, `Json()`, or `Binary()` column takes neither a unique key nor a literal `DEFAULT`: MySQL wants a prefix length for the first and refuses the second.

A `references` declaration becomes a table-level `FOREIGN KEY` in `CREATE TABLE`, and a named `ADD CONSTRAINT` statement when the column is added to a table that already exists. InnoDB parses a `REFERENCES` clause written beside a column and creates nothing, so a clause written there would look like a foreign key while not enforcing anything.

An unsigned integer column has no `tableColumns` declaration that produces it, so one already in your database reports as drift that a migration won't be able to close. Leave the column out of the model, or move it to a signed type.

### Schema changes commit as they run

MySQL has no transactional DDL. Every schema statement commits the moment it runs, whatever the surrounding transaction does. This has two consequences.

`sustained rehearse` refuses MySQL in place, because a rollback would have no effect and the run would report a database as unchanged when it had changed. Point it at a scratch database instead:

```python
# sustained_config.py
def get_rehearsal_connection():
    return pymysql.connect(host='db.internal', user='app', database='app_rehearsal')
```

Through the API, that is `migrator.rehearse(scratch=True)` on a migrator built over the throwaway connection.

A migration that fails halfway leaves the statements before it applied. The run records a failure row against that migration, `validate()` refuses the next run while the row is there, and `repair()` clears it once you have checked what landed. `sustained script up` prints every statement the run would have executed, so you can read down the list and find where it stopped.

## Presto and Trino

The Presto dialect renders double-quoted identifiers, `OFFSET` before `LIMIT`, and `?` placeholders, which matches the `trino` DB-API package. Presto is a query federation engine, so writes are limited: upserts, identity columns, and RETURNING raise `DialectError`.

```python
import trino
from sustained.dialects import Dialects

Event.set_dialect(Dialects.PRESTO)
Event.bind(trino.dbapi.connect(host='presto.internal', port=8080, catalog='hive', schema='web'))

counts = (Event.query()
    .select('page')
    .count('*', alias='views')
    .groupBy('page')
    .run()
)
```

## AWS Athena

Athena runs a Trino-based engine over files in S3, so the dialect inherits Presto's query behavior and adds Athena's storage model: `%s` placeholders matching pyathena, `MERGE` upserts on Iceberg tables, Athena type spellings (`INT`, `STRING`, `DOUBLE`, `DECIMAL`), and `TableOptions` for `PARTITIONED BY`, `LOCATION`, and `TBLPROPERTIES` clauses. Sustained never calls boto3 itself: pyathena wraps the boto3 query lifecycle behind the DB-API cursor.

```python
from pyathena import connect
from sustained.dialects import Dialects

Event.set_dialect(Dialects.ATHENA)
Event.bind(connect(
    s3_staging_dir='s3://bucket/athena-results/',
    region_name='us-east-1',
))

deploys = Event.query().where('name', '=', 'deploy').run()
```

Every `run()` is one Athena query execution with its own scan cost and latency, so patterns that are cheap elsewhere add up here: eager loading costs one execution per relation, and `cursor_page()` one per page. Athena tables have no constraints, indexes, or transactions. See [Schema and Migrations](./schema#athena) for how DDL and the migrator handle that, and for what requires Iceberg tables.

## DuckDB

DuckDB supports native `ILIKE`, `QUALIFY`, `DISTINCT ON`, `ON CONFLICT` upserts, RETURNING, CTAS, and in-place column type changes with `SET DATA TYPE`. Identifiers quote with double quotes and placeholders are `?`, matching the `duckdb` module's DB-API interface. `autoincrement` raises `DialectError` because DuckDB has no identity columns; use a sequence through raw SQL.

```python
import duckdb
from sustained.dialects import Dialects

Event.set_dialect(Dialects.DUCKDB)
Event.bind(duckdb.connect('analytics.db'))

top = (Event.query()
    .select('page')
    .select_window('ROW_NUMBER', 'rank', partition_by=['site'], order_by=['views'])
    .qualify('rank <= 3')
    .run()
)
```

## Enum columns

An `Enum` column declares its values once, and each engine holds the column to them with the mechanism it has:

| Dialect | Strategy | Renders |
| --- | --- | --- |
| `POSTGRES` | named type | `CREATE TYPE post_status AS ENUM (...)`, referenced by the column. Values append in place with `ALTER TYPE ... ADD VALUE`. |
| `DUCKDB` | named type | `CREATE TYPE ... AS ENUM (...)`. Appending a value in place raises `DialectError`. |
| `MYSQL` | inline | `ENUM('draft', 'published')` written into the column type. Value changes restate the list with `MODIFY COLUMN`. |
| `DEFAULT`, `MSSQL` | CHECK constraint | A VARCHAR sized to the longest value, held to the list by `CONSTRAINT ck_<table>_<column>_enum CHECK (col IN (...))`. |
| `PRESTO`, `ATHENA` | refused | `DialectError` at DDL time, because neither engine can enforce the list. |

On Postgres, `ALTER TYPE ... ADD VALUE` rolls back inside a transaction on PostgreSQL 12 and later, which is what lets `rehearse` prove a migration that carries one. See [Schema and Migrations](./schema#enum-columns) for how enum changes generate.

## Writing dialect-portable code

If you build queries through the builder's methods rather than raw SQL, one model definition serves every dialect: quoting, placeholders, booleans, `LIMIT` spelling, upsert syntax, and function names (`NOW()`, `LENGTH()`) all follow `set_dialect()`. The differences that cannot be papered over raise `DialectError` with a message naming the alternative, so porting is mostly a matter of running your test suite and reading the errors it raises.
