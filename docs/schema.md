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

## Validation and Repair

The tracking table records more than the id: a sequence number fixes the apply order, a SHA-256 checksum of the up statements pins the migration's contents, and each row carries the apply timestamp, the execution time in milliseconds, and a success flag. Tracking tables written by earlier versions upgrade in place on first use.

`up()` validates before it runs and raises `MigrationError` when the history and the registry disagree:

- An applied migration was edited after it ran (its checksum changed).
- An applied id is not registered with this migrator.
- A pending migration is ordered before an applied one.
- A failed attempt is on record.

```python
migrator.validate()                     # same checks, on demand
migrator.validate(raise_on_problems=False)  # -> list of problem strings
migrator.up(allow_out_of_order=True)    # accept a late-arriving migration
migrator.up(validate=False)             # skip the checks entirely
```

`repair()` brings the tracking table back in line: it deletes rows left by failed attempts and rewrites stored checksums after an intentional edit, including null checksums on rows written before checksums existed. Repair only fixes bookkeeping; schema changes a failed attempt left behind need manual cleanup first.

```python
migrator.repair()
# ["removed the failed attempt of 'add_flag'",
#  "updated the stored checksum of 'create_users'"]
```

Checksums cover the exact SQL text, so reformatting a migration counts as an edit; run `repair()` to accept it. Callable steps have no SQL to hash and record a null checksum, which validation skips; pass `Migration(..., checksum='...')` to pin one yourself.

On engines without transactions, a failing step writes a row with the success flag off, so the interrupted run is visible. Validation then blocks `up()` until you clean up and run `repair()`.

## Concurrency

While a run is in progress, the migrator holds an exclusive advisory lock named after the tracking table, so two application instances deploying at once queue instead of racing each other's DDL. Postgres uses `pg_advisory_lock`, MSSQL uses `sp_getapplock`; both are session-scoped and release on disconnect. SQLite and DuckDB serialize writers on their own. Athena has no lock to take, so run one migrator at a time there.

## Adopting an Existing Database

`baseline()` records migrations as applied without running them, for a database whose schema already matches. Rows carry real checksums, so validation still catches later edits, and a null execution time marks them as never having run. Later `up()` calls apply only what comes after.

```python
migrator.baseline('create_users')   # record up to and including this id
migrator.up()                       # apply only the rest
```

## Inspecting Drift Before Applying

`migrator.plan()` returns the migration `sync()` would generate, without registering or applying it, so its statements can be reviewed first. It returns `None` when the schema is current and takes the same options as `sync()`.

```python
migration = migrator.plan([User])
if migration is not None:
    print(migration.up)
```

At a lower level, `diff_schema()` reports every difference without touching anything, and `autogenerate()` builds the migration directly.

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
- **Type and nullability changes migrate per dialect.** Postgres, MSSQL, and DuckDB alter in place with reversible down steps; Postgres casts take a hint through `type_casts={'table.col': 'col::integer'}`. SQLite rebuilds the table (create new, copy rows, replace), which is not reversible. Pass `ignore_changed_columns=True` to skip them entirely.
- **NOT NULL needs a value for existing rows.** Adding or tightening to NOT NULL requires a `default` or a `backfill` value on the ColumnDef; generation emits add-nullable, UPDATE backfill, SET NOT NULL, or folds the backfill into a SQLite rebuild. New primary key or autoincrement columns cannot be added with ALTER TABLE.
- The migration tracking table is excluded from diffing, and `exclude_tables` protects any other tables Sustained does not manage.

Renames cannot be detected from the catalog, so pass hints: `sync(models, renames={'users.name': 'full_name'}, table_renames={'old': 'new'})` emits reversible RENAME statements instead of a destructive drop-plus-add.

Primary key, foreign key, column-level unique, and default differences are reported as constraint notes in the diff but never auto-migrated.

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

Definitions support composite primary keys (mark several columns `primary_key=True`), `unique`, literal or raw `Expression` defaults, foreign keys through `references='table.column'`, and `backfill` values for NOT NULL migrations. Models also declare named indexes, which `create_table()` and generated migrations create and keep in sync:

```python
from sustained.schema import Index

class User(Model):
    tableName = 'users'
    tableColumns = {...}
    indexes = [Index('ix_users_email', 'email', unique=True)]
```

`autoincrement` requires a single integer primary key; DuckDB and Presto raise because they have no identity columns. A model with `tableColumns` also gets strict column-name access automatically: a typo'd column raises `AttributeError`.

## Generating and Running DDL Directly

```python
User.create_table_sql()   # dialect-specific CREATE TABLE
User.create_table(conn)   # execute it
User.drop_table(conn)     # DROP TABLE IF EXISTS
```

Types map per dialect: `BIT`, `NVARCHAR`, and `DATETIME2` on MSSQL; `JSONB` and identity columns on Postgres; plain `INTEGER PRIMARY KEY` rowid behavior on the default dialect. Schema introspection reads SQLite's PRAGMA tables on the default dialect and `information_schema.columns` elsewhere.

## Athena

Athena tables are files on S3, so the rules above bend in specific ways on `Dialects.ATHENA`:

- **No constraints.** A column with `primary_key`, `unique`, `default`, `references`, `nullable=False`, or `autoincrement` raises `DialectError` at DDL time. Declare Athena model columns plain and nullable. Declared indexes also raise; partition instead.
- **Table options.** Athena tables need storage clauses. Declare them with `TableOptions` on the model:

```python
from sustained.schema import Integer, String, TableOptions, Timestamp

class Event(Model):
    tableName = 'events'
    tableColumns = {
        'id': Integer(),
        'name': String(120),
        'created_at': Timestamp(),
    }
    tableOptions = TableOptions(
        location='s3://bucket/warehouse/events/',
        partitioned_by=['day(created_at)'],
        properties={'table_type': 'ICEBERG'},
    )
```

This renders `PARTITIONED BY (day(created_at)) LOCATION '...' TBLPROPERTIES ('table_type'='ICEBERG')` after the column list. Partition entries pass through as written, so Iceberg transforms work. Every other dialect raises when `tableOptions` is set.

- **Migrations run without transactions.** Athena has none, so the migrator runs each step bare and never calls rollback. A failing multi-step migration can leave partial changes that need manual cleanup. The tracking table needs its own storage location:

```python
migrator = Migrator(
    conn, [], dialect=Dialects.ATHENA,
    tracking_table_options=TableOptions(
        location='s3://bucket/warehouse/sustained_migrations/',
        properties={'table_type': 'ICEBERG'},
    ),
)
```

Reverting migrations deletes tracking rows, which requires the tracking table to be Iceberg.

- **Column changes use Iceberg rules.** `sync()` adds columns with `ALTER TABLE ... ADD COLUMNS` and changes types with `CHANGE COLUMN`, which Iceberg only allows for widenings such as `INT` to `BIGINT`. Renames, nullability changes, and `type_casts` hints raise; write those by hand in a `Migration`.

Upserts (`onConflict().merge()`), `UPDATE`, `DELETE`, and `down()` reverts all depend on Iceberg tables. Plain Hive external tables are read-and-append only.

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

## Migrations as SQL Files

Migrations can live as plain SQL files instead of Python objects. `load_migrations()` reads a directory of `<id>.up.sql` files, each optionally paired with a `<id>.down.sql`, and returns `Migration` objects ordered by id, so file naming fixes the apply order. Statements split at line-ending semicolons; semicolons inside string literals stay intact.

```
migrations/
  001_create_users.up.sql
  001_create_users.down.sql
  002_add_flag.up.sql
```

```python
from sustained.migration_files import load_migrations

migrator = Migrator(conn, load_migrations('migrations'))
migrator.up()
```

Empty up or down files, a down file without its up file, and `.sql` files that fit neither naming pattern raise `ValueError`. Files without a `.sql` extension are ignored, so a README can live alongside the migrations.

## Repeatable Migrations

Views, functions, and seed data are replaced rather than evolved, so they fit badly in versioned migrations. A `<id>.repeat.sql` file is a repeatable migration: it runs whenever its checksum is new or changed, after every versioned migration, on every `migrate` including targeted ones.

```sql
-- active_users.repeat.sql
DROP VIEW IF EXISTS active_users;
CREATE VIEW active_users AS SELECT * FROM users WHERE active = 1;
```

Edit the file and the next `migrate` re-runs it; keep the SQL replace-safe (`CREATE OR REPLACE`, or a drop first). The tracking table holds one row per repeatable, updated in place on re-runs with its original sequence number. Repeatables have no down file, and `down` never touches them. `baseline` records every repeatable at its current checksum, so adopting an existing schema does not re-run objects it already holds.

`status` (and `Migrator.statuses()`) reports each migration as `applied`, `pending`, or `changed`; `changed` marks a repeatable whose contents differ from its last run. In Python, pass `Migration(id, up=..., repeatable=True)`; a repeatable with a callable step needs an explicit `checksum` so re-runs can be detected. An id may not have both an up file and a repeat file.

## Placeholders

SQL files may hold `${key}` placeholders, filled from a mapping at load time:

```sql
-- 003_grant.up.sql
GRANT SELECT ON users TO ${reader};
```

```python
migrations = load_migrations('migrations', placeholders={'reader': 'app_ro'})
```

Passing a mapping, even an empty one, turns substitution on: a `${key}` with no value then raises `ValueError` naming the file and the key, and `$${` escapes to a literal `${`. With no mapping, files load untouched. Keys are identifiers; there are no expressions, defaults, or environment lookups.

Substitution happens before checksums compute, so the checksum covers the SQL that actually ran. Changing a placeholder value after a migration applied flags a checksum mismatch, because different SQL was applied; run `repair()` if the new value is intentional.

## Command Line

The `sustained` console script (also `python -m sustained`) runs migrations from the shell. It imports a config module, `sustained_config` by default or `--config mymodule`, from the current directory:

```python
# sustained_config.py
import sqlite3

def get_connection():
    return sqlite3.connect('app.db')

migrations_dir = 'migrations'
# optional: migrations = [...], placeholders = {...},
# dialect = 'postgres', table = '...',
# tracking_table_options = TableOptions(...)
```

```console
$ sustained status
$ sustained migrate                 # --target ID, --no-validate, --allow-out-of-order
$ sustained down                    # --steps N or --to ID
$ sustained validate                # exits 1 when problems exist
$ sustained repair
$ sustained script down             # print the SQL without running it
$ sustained baseline 001_create_users
```

Commands exit 0 on success and 1 on failure, with errors on stderr, so they slot into deploy pipelines.

## Offline Review and Async

`migrator.script('up')` renders every statement a run would execute, including tracking bookkeeping, without touching the database, for review or DBA handoff; `script('down')` renders the rollback. For async services, `AsyncMigrator` in `sustained.aio_migrations` runs the same `Migration` objects on an `AsyncAdapter` with the same `up`, `down`, `down_to`, `status`, `statuses`, `validate`, `repair`, and `baseline` surface; callable steps receive the adapter and are awaited.
