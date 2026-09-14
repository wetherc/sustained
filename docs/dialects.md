---
layout: default
title: SQL Dialects
---

Sustained compiles the same query for every database engine it supports: you set the dialect once per model, usually at application startup, and every query, DDL statement, and migration for that model will then in that engine's SQL:

```python
from sustained.dialects import Dialects

User.set_dialect(Dialects.POSTGRES)
```

Recompiling the query to target a different SQL engine is a one-line configuration change (assuming feature parity between engines for the query you have built). If an engine lacks a feature, the query raises a `DialectError` when it builds.

## Dialects, drivers, and placeholders

To execute queries, bind a DB-API 2.0 connection whose parameter style matches the dialect's placeholder. `to_sql()` renders that placeholder, and `run()` passes the parameters straight to the driver, so a mismatch will fail at execution time.

| Dialect | Engine | Recommended driver | Placeholder | Quoting |
| --- | --- | --- | --- | --- |
| `Dialects.DEFAULT` | ANSI SQL, SQLite | `sqlite3` (standard library) | `?` | none |
| `Dialects.POSTGRES` | PostgreSQL | `psycopg` or `psycopg2` | `%s` | `"name"` |
| `Dialects.MSSQL` | Microsoft SQL Server | `pyodbc` | `?` | `[name]` |
| `Dialects.MYSQL` | MySQL, MariaDB | `PyMySQL` | `%s` | `` `name` `` |
| `Dialects.PRESTO` | Presto, Trino | `trino` or `presto-python-client` | `?` | `"name"` |
| `Dialects.ATHENA` | AWS Athena | `pyathena` | `?` | `"name"` |
| `Dialects.DUCKDB` | DuckDB | `duckdb` | `?` | `"name"` |

Async execution wraps a driver in an adapter instead: `AsyncpgAdapter` for asyncpg, `AiosqliteAdapter` for aiosqlite, and `DbApiAsyncAdapter` for any synchronous driver in the table. See [Executing Queries](./executing#async-execution).

## Default (ANSI, SQLite)

The default dialect renders plain ANSI SQL with unquoted identifiers and `?` placeholders. Sustained treats this dialect as executing against SQLite.

```python
import sqlite3
from sustained import Model

class User(Model):
    tableName = 'users'

User.bind(sqlite3.connect('app.db'))
users = User.query().where('active', '=', True).run()
```

## PostgreSQL

Postgres supports the largest set of features: native `ILIKE`, `DISTINCT ON`, `RETURNING`, `ON CONFLICT` upserts, `for_update()` row locking, identity columns for `autoincrement`, `JSONB` for the `Json` type, and in-place `ALTER COLUMN` migrations with `USING` cast hints. Migration runs take a `pg_advisory_lock`, so concurrent deploys queue behind one another. Placeholders are passed as `%s`.

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

MSSQL quotes identifiers with square brackets and uses `?` placeholders. Booleans render as `1`/`0`, `Boolean` columns as `BIT`, strings as `NVARCHAR`, and timestamps as `DATETIME2`. `top(n)` renders `TOP n`. `limit()` and `offset()` compile to `OFFSET ... FETCH` (although T-SQL only allows this after `orderBy()`). Upserts render a `MERGE` statement. `NOW()` translates to `GETDATE()` and `LENGTH()` to `LEN()`.

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

RETURNING, CTAS, and `explain()` raise `DialectError`. Use `OUTPUT`, `SELECT INTO`, and SSMS plans through raw SQL instead. Migrations rename with `sp_rename`, alter columns by restating the full definition, and keep an `sp_getapplock` session lock while they run.

## MySQL and MariaDB

The `MYSQL` dialect supports both MySQL and MariaDB; Sustained does not distinguish between the two. Identifiers quote with backticks and placeholders are `%s`. Upserts render `ON DUPLICATE KEY UPDATE`. `for_update()` works, with `SKIP LOCKED` and `NOWAIT` on MySQL 8.0 and later. Migration runs take a `GET_LOCK` session lock.

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

Column types are automatically converted to database-native types:

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

`Timestamp()` maps to `DATETIME` rather than `TIMESTAMP`. MySQL's `TIMESTAMP` is four bytes, stops in 2038, and performs implicit timezone conversion.

RETURNING raises `DialectError`. MariaDB supports it, but MySQL does not. Instead, you can read the row back with a second query, or use `LAST_INSERT_ID()` through raw SQL. `STRING_AGG` raises as well, rather than translating to `GROUP_CONCAT`, whose separator is a keyword and not a second argument. `Text()`, `Json()`, or `Binary()` columns will not accept either a unique key or a literal `DEFAULT`.

A `references` declaration becomes a table-level `FOREIGN KEY` in `CREATE TABLE`, and a named `ADD CONSTRAINT` statement when the column is added to a table that already exists.

`tableColumns` will never produce an unsigned integer column, so one already in your database reports as unrecoverable drift. Leave the column out of the model, or move it to a signed type to resolve this.

### Schema changes commit as they run

MySQL has no transactional DDL, so every schema statement commits the moment it runs, whatever the surrounding transaction does.

`sustained rehearse` refuses to run against MySQL since it cannot safely execute the migration without committing the changes. Point it at a scratch database instead:

```python
# sustained_config.py
def get_rehearsal_connection():
    return pymysql.connect(host='db.internal', user='app', database='app_rehearsal')
```

Through the API, that is `migrator.rehearse(scratch=True)` on a migrator built over the throwaway connection.

The migration run will record any failure against that migration, `validate()` will refuse the next run while the row is there, and `repair()` will clear it once you have checked what landed. `sustained script up` prints every statement the run would have executed, so you can read down the list and find where it stopped.

## Presto and Trino

The Presto dialect renders double-quoted identifiers, `OFFSET` before `LIMIT`, and uses `?` placeholders. Presto is a query federation engine, so writes are limited: upserts, identity columns, and RETURNING raise `DialectError`.

```python
import trino
from sustained.dialects import Dialects

Event.set_dialect(Dialects.PRESTO)
Event.bind(trino.dbapi.connect(
    host='presto.internal',
    port=8080,
    catalog='hive',
    schema='web'))

counts = (Event.query()
    .select('page')
    .count('*', alias='views')
    .groupBy('page')
    .run()
)
```

## AWS Athena

Athena runs a Trino-based engine over files in S3, so the dialect inherits Presto's query behavior and adds Athena's storage model: `?` placeholders, `MERGE` upserts on Iceberg tables, Athena type spellings (`INT`, `STRING`, `DOUBLE`, `DECIMAL`), and `TableOptions` for `PARTITIONED BY`, `LOCATION`, and `TBLPROPERTIES` clauses. `String(n)` and `Text()` both render `STRING`, because Iceberg tables reject `VARCHAR`. Sustained never calls boto3 itself, because pyathena wraps the boto3 query lifecycle behind the DB-API cursor.

Set `pyathena.paramstyle = "qmark"` before you run a parameterized query. Sustained passes parameters as a tuple, and pyathena's default pyformat style takes a dict only. With qmark, pyathena sends the tuple as native Athena execution parameters. This needs pyathena 3 or later.

Athena's API only takes execution parameters as strings, so `run()` converts each value: numbers through `str()`, booleans to `true`/`false`. Athena infers the value's type from the position of its placeholder, so a converted number still compares against a numeric column. `None` becomes a literal `NULL` in the statement. Binary values raise `DialectError`. The conversion runs inside `run()` and the migrator; if you execute `to_sql()` output yourself, pass it through `compiler.prepare_execution(sql, params)` first.

```python
import pyathena
from pyathena import connect
from sustained.dialects import Dialects

pyathena.paramstyle = 'qmark'

Event.set_dialect(Dialects.ATHENA)
Event.bind(connect(
    s3_staging_dir='s3://bucket/athena-results/',
    region_name='us-east-1',
))

deploys = Event.query().where('name', '=', 'deploy').run()
```

Every `run()` is one Athena query execution with its own scan cost and latency, so query patterns that are cheap in most other database engines can add up here: eager loading costs one execution per relation, and `cursor_page()` one execution per page. Athena tables have no constraints, indexes, or transactions. See [Schema and Migrations](./schema#athena) for how DDL and the migrator handle that, and for what requires Iceberg tables.

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

An `Enum` column declares its allowed values, and those values are enforced by database-specific mechanisms:

| Dialect | Strategy | Renders |
| --- | --- | --- |
| `POSTGRES` | named type | `CREATE TYPE post_status AS ENUM (...)`, referenced by the column. Values append in place with `ALTER TYPE ... ADD VALUE`. |
| `DUCKDB` | named type | `CREATE TYPE ... AS ENUM (...)`. Appending a value in place raises `DialectError`. |
| `MYSQL` | inline | `ENUM('draft', 'published')` written into the column type. Value changes restate the list with `MODIFY COLUMN`. |
| `DEFAULT`, `MSSQL` | CHECK constraint | A VARCHAR sized to the longest value, constrained to the list by `CONSTRAINT ck_<table>_<column>_enum CHECK (col IN (...))`. |
| `PRESTO`, `ATHENA` | refused | `DialectError` at DDL time, because neither engine can enforce the list. |

On PostgreSQL 12 and later, `ALTER TYPE ... ADD VALUE` rolls back inside a transaction, so `rehearse` can test a migration that contains one. See [Schema and Migrations](./schema#enum-columns) for how enum changes generate.

## Column comments

A column's `comment` is stored specific to the database engine as well:

| Dialect | Stores | Renders |
| --- | --- | --- |
| `POSTGRES`, `DUCKDB` | yes | `COMMENT ON COLUMN ... IS '...'` after the table or the added column. Introspection reads it back, from `pg_description` and `duckdb_columns()`. |
| `MYSQL` | yes | `COMMENT '...'` inside the column definition. Changes restate the column with `MODIFY COLUMN`. Read back from `information_schema`. |
| `PRESTO` | yes | `COMMENT '...'` inside the column definition, changed with `COMMENT ON COLUMN`. Read back from `information_schema`. |
| `ATHENA` | at creation | `COMMENT '...'` inside `CREATE TABLE`. Athena cannot change a comment in place, so a drifted comment raises `DialectError`. |
| `DEFAULT`, `MSSQL` | no | Nothing. The comment stays on the model as documentation and never drifts. |

See [Column comments](./schema#column-comments) for how drift generates.

## Writing dialect-portable code

If you build queries through the builder's methods rather than raw SQL, one model definition is capable of serving every dialect: quoting, placeholders, booleans, `LIMIT` spelling, upsert syntax, and function names (`NOW()`, `LENGTH()`) all follow `set_dialect()`. Database features without clear analogues raise `DialectError` with a message naming the alternative, so porting is mostly a matter of running your test suite and reading the errors it raises.
