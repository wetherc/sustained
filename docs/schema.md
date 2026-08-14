---
layout: default
title: Schema and Migrations
---

Models can declare their physical shape and generate DDL from it, and the migration runner applies ordered schema changes.

## Typed Columns

Declare `tableColumns` as a dict of column name to a typed definition. The factories are `Integer`, `BigInteger`, `String(length)`, `Text`, `Boolean`, `Float`, `Numeric(precision, scale)`, `Date`, `Timestamp`, and `Json`.

```python
from sustained import Model
from sustained.schema import Boolean, Integer, String, Timestamp
from sustained.types import Expression

class User(Model):
    tableName = 'users'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'email': String(120, unique=True, nullable=False),
        'active': Boolean(default=True),
        'created_at': Timestamp(default=Expression('CURRENT_TIMESTAMP')),
    }
```

Definitions support composite primary keys (mark several columns `primary_key=True`), `unique`, literal defaults or raw `Expression` defaults, and foreign keys through `references='table.column'`. `autoincrement` requires a single integer primary key; DuckDB and Presto raise because they have no identity columns.

A model with `tableColumns` also gets strict column-name access automatically: a typo'd column raises `AttributeError`.

## Generating and Running DDL

```python
User.create_table_sql()               # dialect-specific CREATE TABLE
User.create_table(conn)               # execute it
User.drop_table(conn)                 # DROP TABLE IF EXISTS
```

Types map per dialect: `BIT`, `NVARCHAR`, and `DATETIME2` on MSSQL; `JSONB` and identity columns on Postgres; plain `INTEGER PRIMARY KEY` rowid behavior on the default dialect.

## Migrations

Migrations are explicit and ordered. Each pairs an id with an up step and an optional down step; steps are a SQL string, a list of statements, or a callable receiving the connection.

```python
from sustained.migrations import Migration, Migrator, create_table_migration

migrations = [
    create_table_migration(User),
    Migration(
        'add_last_login',
        up='ALTER TABLE users ADD COLUMN last_login TEXT',
        down='ALTER TABLE users DROP COLUMN last_login',
    ),
]

migrator = Migrator(conn, migrations)
migrator.up()          # apply all pending
migrator.up(target='create_users')  # stop after a target
migrator.status()      # [(id, applied), ...]
migrator.down()        # revert the newest applied migration
```

Applied ids live in a tracking table that the migrator creates on first use. Each migration runs inside a transaction, so a failing step leaves the schema at the previous migration. There is no automatic diffing against the database catalog; write migrations explicitly or derive create/drop pairs from models with `create_table_migration()`.
