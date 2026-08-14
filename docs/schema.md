---
layout: default
title: Schema and Migrations
---

Sustained manages your database schema from your models. Declare typed columns once, then let the migrator create tables, detect drift, generate migrations, apply them, and roll them back.

## Automated Migration in One Call

`Migrator.sync()` diffs the live database against your models, generates the migration, records it, and applies it. Run it again after changing a model and only the difference is applied. Nothing to hand-write for additive changes.

```python
from sustained import Model
from sustained.migrations import Migrator
from sustained.schema import Integer, String, Text

class User(Model):
    tableName = 'users'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'email': String(120, unique=True, nullable=False),
    }

migrator = Migrator(conn, [])
migrator.sync([User])          # creates the users table

User.tableColumns['bio'] = Text()
migrator.sync([User])          # adds only the bio column
```

## Automated Rollback

Generated migrations are reversible: a created table carries its DROP, an added column carries its DROP COLUMN. Roll back one step, several steps, or down to a known-good migration.

```python
migrator.down()                # revert the newest applied migration
migrator.down(steps=2)         # revert the two newest
migrator.down_to('auto_20260814...')  # revert until this id is newest
```

Every applied migration is recorded in a tracking table, each runs inside a transaction, and a failing step rolls itself back and leaves earlier migrations applied.

## Inspecting Drift Before Applying

`diff_schema()` reports every difference without touching anything. `autogenerate()` builds the migration so you can review its statements before it runs.

```python
from sustained.autogenerate import autogenerate, diff_schema

print(diff_schema(conn, [User]).summary())
# add column users.bio
# drop column users.legacy (destructive)

migration = autogenerate(conn, [User], id='add_bio')
print(migration.up)    # ['ALTER TABLE users ADD COLUMN bio TEXT']
print(migration.down)  # ['ALTER TABLE users DROP COLUMN bio']
```

## Safety Rules

Autogeneration refuses to guess about anything that loses data or fails on populated tables:

- **Drops are opt-in.** Extra tables and columns raise unless `allow_drops=True`. A migration containing drops has no down step, because the dropped data cannot come back.
- **Type changes are never auto-migrated.** A changed type or nullability is reported in the diff and blocks generation until you write that migration by hand or pass `ignore_changed_columns=True`. SQLite cannot alter a column type in place, and a silent rewrite is exactly the change a human should review.
- **Unsafe adds are rejected.** A new NOT NULL column needs a default; new primary key or autoincrement columns cannot be added with ALTER TABLE.
- The migration tracking table is excluded from diffing, and `exclude_tables` protects any other tables Sustained does not manage.

Diffing compares column presence, type, and nullability. Constraint changes (primary keys, unique indexes, foreign keys) are out of scope.

## Typed Columns

`tableColumns` maps column names to typed definitions: `Integer`, `BigInteger`, `String(length)`, `Text`, `Boolean`, `Float`, `Numeric(precision, scale)`, `Date`, `Timestamp`, `Json`.

```python
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

Definitions support composite primary keys (mark several columns `primary_key=True`), `unique`, literal or raw `Expression` defaults, and foreign keys through `references='table.column'`. `autoincrement` requires a single integer primary key; DuckDB and Presto raise because they have no identity columns. A model with `tableColumns` also gets strict column-name access automatically: a typo'd column raises `AttributeError`.

## Generating and Running DDL Directly

```python
User.create_table_sql()   # dialect-specific CREATE TABLE
User.create_table(conn)   # execute it
User.drop_table(conn)     # DROP TABLE IF EXISTS
```

Types map per dialect: `BIT`, `NVARCHAR`, and `DATETIME2` on MSSQL; `JSONB` and identity columns on Postgres; plain `INTEGER PRIMARY KEY` rowid behavior on the default dialect. Schema introspection reads SQLite's PRAGMA tables on the default dialect and `information_schema.columns` elsewhere.

## Hand-Written Migrations

Anything autogeneration will not express, write explicitly. A `Migration` pairs an id with an up step and an optional down step; steps are a SQL string, a list of statements, or a callable receiving the connection. Hand-written and generated migrations share one ordered list and one tracking table.

```python
from sustained.migrations import Migration, Migrator, create_table_migration

migrations = [
    create_table_migration(User),
    Migration(
        'split_name_column',
        up=[
            'ALTER TABLE users ADD COLUMN first_name TEXT',
            "UPDATE users SET first_name = substr(name, 1, instr(name, ' ') - 1) WHERE 1 = 1",
        ],
        down='ALTER TABLE users DROP COLUMN first_name',
    ),
]

migrator = Migrator(conn, migrations)
migrator.up()                        # apply all pending
migrator.up(target='create_users')   # stop after a target
migrator.status()                    # [(id, applied), ...]
```
