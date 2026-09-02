---
layout: default
title: Schema and Migrations
---

Sustained manages your database schema from your models. You declare typed columns once, then the migrator creates tables, detects drift, generates migrations, rehearses them, applies them, and rolls them back.

A schema change is hard to take back once it has run, so the migrator is built around proving one first. This page follows the path a migration takes: generated or hand-written, planned, rehearsed, applied under a lock, checksummed in a tracking table, and reverted.

## Automated migration in one call

`Migrator.up(models=[...])` diffs the live database against your models, generates the migration, records it, and applies it along with everything else pending. Run it again after changing a model and only the difference is applied. Additive changes need nothing hand-written.

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
migrator.up(models=[User])     # creates the users table

User.tableColumns['bio'] = Text()
migrator.up(models=[User])     # adds only the bio column
```

The diff is taken after the pending migrations have run, so it sees the schema they left behind. A generated migration always runs last of the versioned ones, which is why `models` cannot be combined with a `target`.

`Migrator.sync()` did this before version 2.13.0. It still works, prints a deprecation warning, and will be removed in 3.0.

## Automated rollback

Generated migrations are reversible: a created table carries its DROP, and an added column carries its DROP COLUMN. You can roll back one step, several steps, or down to a known-good migration.

```python
migrator.down()                # revert the newest applied migration
migrator.down(steps=2)         # revert the two newest
migrator.down_to('auto_20260814...')  # revert until this id is newest
```

`steps` counts migrations and must be 0 or more. A count of 0 reverts nothing. A negative count raises `ValueError`, and `sustained down --steps -1` stops at the command line.

A revert reads the checksum of every migration it is about to take back, before it reverts the first one. A migration edited after it was applied raises `MigrationError` and nothing is reverted, because the down step in front of you describes the new contents while the database holds the old ones. Restore the migration, run `repair()` to accept the new contents, or pass `allow_changed=True` (`sustained down --allow-changed`) to revert it as it stands now.

A generated migration isn't materialized anywhere outside the actual execution, so its tracking row carries the statements it ran. A later process, on a later deploy, can revert it from that row without having the diff in hand. A rebuild, which has no down step, still cannot be reverted.

Every applied migration is recorded in a tracking table, each runs inside a transaction, and a failing step rolls itself back and leaves earlier migrations applied.

## Validation and repair

The tracking table records the run id, a sequence number that deterministically sets the apply order, a SHA-256 checksum of the up statements pins the migration's contents, the apply timestamp, the execution time in milliseconds, and a success flag. Tracking tables written by earlier versions upgrade in place on first use.

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

`repair()` brings the tracking table back in line: it deletes rows left by failed attempts and rewrites stored checksums after an intentional edit, including null checksums on rows written before checksums existed. Repair only fixes bookkeeping. Schema changes that a failed attempt left behind need manual cleanup first. Repeatables keep their stored checksums, so a changed repeatable is scheduled to re-run and the next `up()` runs it.

```python
migrator.repair()
# ["removed the failed attempt of 'add_flag'",
#  "updated the stored checksum of 'create_users'"]
```

Checksums are computed on the exact SQL text, so reformatting a migration counts as an edit; run `repair()` to accept it. Callable steps have no SQL to hash and record a null checksum, which validation skips. Pass `Migration(..., checksum='...')` to pin one yourself. That argument is for callable steps only: on a step made of SQL it would replace the hash of the statements and hide every later edit, so it raises `ValueError`.

On engines without transactions, a failing step writes a row with the success flag off, so the interrupted run is visible. Validation then blocks `up()` until you clean up and run `repair()`.

## Concurrency

While a run is in progress, the migrator holds an exclusive advisory lock named after the tracking table, so two application instances deploying at once queue instead of racing each other's DDL. Postgres uses `pg_advisory_lock`, MSSQL uses `sp_getapplock`, and MySQL uses `GET_LOCK`. All three are session-scoped and release on disconnect. MySQL and MSSQL report a lock they did not grant in the value they return instead of raising, so the migrator reads that value and raises `MigrationError` when the lock is not held. SQLite and DuckDB serialize writers on their own. Athena has no lock to take, so run one migrator at a time there.

## Adopting an existing database

`baseline()` records migrations as applied without running them, for a database whose schema already matches. Rows carry real checksums, so validation still catches later edits, and a null execution time marks them as never having run. Later `up()` calls apply only what comes after.

```python
migrator.baseline('create_users')   # record up to and including this id
migrator.up()                       # apply only the rest
```

## Inspecting drift before applying

`migrator.plan()` returns the migration `up(models=[...])` would generate, without registering or applying it, so you can review its statements first. It returns `None` when the schema is current, and it takes the same diff options.

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

## Safety rules

Autogeneration refuses to guess about anything that loses data or fails on populated tables:

- **A NOT NULL column needs a value for the rows already there.** Adding one with no default and no `backfill` is refused, since the rows already in the table would have no value. An empty table has no such rows, so the column is added there.
- **Drops are opt-in.** Extra tables and columns are left alone unless you pass `allow_drops=True`, which generates the drops. A migration containing drops has no down step, because the dropped data cannot come back. `autogenerate()` called directly still raises on undeclared objects; the migrator passes `ignore_undeclared=True`, since a database that hand-written migrations also touch may have undeclared tables.
- **Type and nullability changes migrate per dialect.** Postgres, MySQL, MSSQL, and DuckDB alter in place with reversible down steps, and Postgres casts take a hint through `type_casts={'table.col': 'col::integer'}`. SQLite rebuilds the table (create new, copy rows, replace), which is not reversible. The rebuild drops the old table, which SQLite refuses while rows in another table point at it, so the steps run between `PRAGMA foreign_keys = OFF` and `PRAGMA foreign_keys = ON`. SQLite ignores both statements inside an open transaction, so a migration that carries them is generated with `transactional=False` and the migrator runs it outside one. Run it on a connection in autocommit. A rebuild that nothing points at needs no pragma and keeps its transaction. Because the guarded rebuild runs bare, a step that fails leaves the steps before it applied, leaves the copy table `<table>_sustained_new` behind, and leaves foreign key enforcement off on that connection. Turn it back on with `PRAGMA foreign_keys = ON`, or open a new connection, then finish or undo the rest by hand and run `repair()`. Presto and Trino can do neither, so generation raises `DialectError` there and you write the migration by hand. Columns and indexes the models do not declare survive the rebuild unless you pass `allow_drops=True`. An index on an expression is invisible to introspection, so a rebuild loses it and you have to recreate it by hand. Pass `ignore_changed_columns=True` to skip changed columns entirely.
- **NOT NULL needs a value for existing rows.** Adding or tightening to NOT NULL requires a `default` or a `backfill` value on the ColumnDef, on the rebuild path as well as the ALTER TABLE one. Generation emits add-nullable, UPDATE backfill, SET NOT NULL, or folds the backfill into a SQLite rebuild. New primary key or autoincrement columns cannot be added with ALTER TABLE.
- **Statements that remove data need a rehearsal.** `up()` refuses to run a DROP, a column drop, or a TRUNCATE until a passing rehearsal has proved that exact set of statements. `up(unrehearsed=True)` applies them anyway. See [Rehearsal logging and tracking](#rehearsal-logging-and-tracking).
- **Your own rules run too.** Guards read every statement an up run would apply, generated or hand-written, and can block it. Down runs are not checked. See [Guards](#guards).
- The tracking table and the rehearsal table are excluded from diffing, and `exclude_tables` protects any other tables Sustained does not manage.

Renames cannot be detected from the catalog, so pass hints: `up(models=models, renames={'users.name': 'full_name'}, table_renames={'old': 'new'})` emits reversible RENAME statements instead of a destructive drop-plus-add.

Foreign keys and CHECK constraints declared in `tableConstraints` are diffed and migrated; see [Table constraints](#table-constraints). Primary key set changes, column-level unique, and default differences are reported as constraint notes in the diff, but never auto-migrated.

## Typed columns

`tableColumns` maps column names to typed definitions: `Integer`, `BigInteger`, `String(length)`, `Text`, `Boolean`, `Float`, `Numeric(precision, scale)`, `Date`, `Timestamp`, `Binary`, `Json`, and `Enum(*values, name=...)`.

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

`autoincrement` requires a single integer primary key. DuckDB and Presto raise `DialectError` because they have no identity columns. A model with `tableColumns` also gets strict column-name access automatically, so a typo'd column raises `AttributeError`.

## Enum columns

`Enum` declares a column whose values come from a named, ordered list:

```python
from sustained.schema import Enum, Integer

class Post(Model):
    tableName = 'posts'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'status': Enum(
            'draft', 'published', 'archived',
            name='post_status', nullable=False, default='draft',
        ),
    }
```

Pass the values as strings, or pass one Python `enum.Enum` class whose member values are strings. Hydrated values stay plain strings either way; Sustained never coerces them back into the Python class. A `default` is checked against the values when the column is declared.

`name` is required. A Postgres enum is a database object with its own identity, so an explicit name gives the diff something stable to compare, and it lets two models share one type: the same name with the same values in two models is one type, and the same name with different values raises `ValueError`.

Rendering follows the dialect. Postgres and DuckDB create a named type with `CREATE TYPE ... AS ENUM` and reference it by name. MySQL writes the value list into the column type as `ENUM('draft', 'published', ...)`. ANSI, SQLite, and MSSQL have no enum type, so the column renders as a VARCHAR sized to the longest value, held to the list by a CHECK constraint named `ck_<table>_<column>_enum`. Presto and Athena refuse the column with `DialectError`, because neither engine can enforce it. [SQL Dialects](./dialects#enum-columns) has the full table.

On the dialects with a named type, `CREATE TYPE` renders before the table that uses it, and dropping the table drops the type with it once no remaining model references it, under the same `allow_drops=True` the table drop needs. `DROP TYPE` is destructive: `plan` labels it, `no_drops()` blocks it, and the rehearsal gate covers it.

### Changing an enum's values

Appending a value to the model generates the change: `ALTER TYPE ... ADD VALUE` on Postgres, a restated value list through `MODIFY COLUMN` on MySQL, and a re-created CHECK constraint on the check-strategy dialects. On Postgres the generated migration is irreversible, because Postgres has no `DROP VALUE`; its `down` is `None`. PostgreSQL 12 and later roll `ADD VALUE` back inside a transaction, which is what lets `rehearse` prove it; the [support policy](./support) states that floor. DuckDB cannot append to a type in place and refuses.

Removing or reordering values refuses with a recipe instead of generating: create a new type, move the column over with a `USING` cast, then drop the old type. Converting an existing VARCHAR column to an enum works through the same `type_casts` hints as any other Postgres type change.

## Column comments

Every column definition takes a `comment`, a short description stored in the database's own catalog where the engine has a place for one:

```python
class User(Model):
    tableName = 'users'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'email': String(120, nullable=False, comment='Login address, unique per tenant'),
    }
```

Where the comment lands follows the dialect. MySQL, MariaDB, Presto, Trino, and Athena write it inside the column definition, so `CREATE TABLE` and `ADD COLUMN` carry it in the same statement. Postgres and DuckDB store it with a `COMMENT ON COLUMN` statement rendered after the table or the added column. ANSI, SQLite, and SQL Server have no column comments in the catalog; the comment stays on the model as documentation and renders nothing.

Introspection reads the comments back on every dialect that stores them, and generation keeps them in sync. A comment changed or removed out of band shows up in `plan` and `diff`, and the generated migration writes the declared text back, with a down step that restores what the database had. A dialect that stores no comments never reports comment drift, and neither does a catalog read that could not see them, so absence is never mistaken for removal.

Two edges are deliberate. The comment is not part of a step's identity when it is absent, so adding `comment` support changed no existing migration checksums. And Athena stores a comment at `CREATE TABLE` but cannot change one in place, so a drifted comment there becomes a constraint note and the rest of the migration still generates; recreate the table or write the `CHANGE COLUMN` statement by hand. A hand-written `set_column_comment` step still raises `DialectError` on Athena.

Hand-written migrations set or clear a comment with the [`set_column_comment` step](./reference/migrations#typed-ddl-steps).

## Table constraints

Models declare named CHECK and FOREIGN KEY constraints in `tableConstraints`, parallel to `indexes` and `tableOptions`:

```python
from sustained.schema import Check, ForeignKey, Integer, Numeric

class Ticket(Model):
    tableName = 'tickets'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'show_id': Integer(nullable=False),
        'price': Numeric(10, 2),
    }
    tableConstraints = [
        Check('ck_tickets_price_positive', 'price > 0'),
        ForeignKey('fk_tickets_show', 'show_id', 'shows.id', on_delete='CASCADE'),
    ]
```

Every constraint takes a name, because diffs and down steps address a constraint by its name. A `Check` expression is raw SQL and renders as written, so quote any identifiers inside it yourself.

A `ForeignKey` takes its columns as a string or a sequence, and its targets as `'table.column'` strings, one per constrained column, all pointing at one table. `on_delete` and `on_update` accept `CASCADE`, `SET NULL`, `RESTRICT`, `NO ACTION`, and `SET DEFAULT`. For a single column with no actions, the `references='table.column'` shorthand on the column definition does the same thing. There is no Unique constraint object: `Index(name, *columns, unique=True)` already covers it.

A table the database lacks is created without its foreign keys on any engine that takes `ALTER TABLE ADD CONSTRAINT`: Postgres, MySQL, MariaDB, and MSSQL. Every key follows as its own statement once all the new tables exist, so two new tables may point at each other, and a table may point at itself. SQLite and DuckDB take no constraint after the table is made, so their keys stay inside `CREATE TABLE` and the new tables are created in dependency order instead. A reference cycle cannot be ordered; it is reported as a constraint note, and you write that migration by hand.

Constraint differences generate migrations. A constraint the model declares and the database lacks generates `ADD CONSTRAINT`, with the matching drop as the down step. A changed foreign key generates a drop plus an add under `allow_drops=True`; the down step restores the introspected key when its target is known, and the migration is irreversible otherwise. Constraints in the database that no model declares follow the same rules as extra indexes: they block generation unless `ignore_undeclared` is set, and they drop only under `allow_drops=True`.

A CHECK constraint is the exception. It never blocks generation. Engines rewrite a check expression on the way in, so a check the models do declare can read back as one they do not, and a refusal there would stop a diff that has nothing wrong with it. An undeclared check comes back as a note on the diff instead, and `allow_drops=True` still drops it.

Checks diff on every engine whose catalog reports them: Postgres, MySQL 8.0.16 and later, MariaDB, MSSQL, DuckDB, and SQLite. Presto and Athena hold no CHECK constraints, so nothing diffs there. An engine too old for `information_schema.check_constraints` reads no checks, and a declared check then stays undiffed rather than reporting as missing.

Check expressions compare normalized, because engines rewrite them: Postgres stores `price > 0` as `((price > 0))`, and keyword case and whitespace vary. A difference that survives normalization is reported as a constraint note on Postgres rather than generating a drop, so a cosmetic rewrite never costs you a constraint. SQLite cannot add or drop a table constraint in place, so those changes route through the same table rebuild as its column changes.

Presto and Athena enforce no table constraints and raise `DialectError` when a model declares them there. Primary key set changes stay reported as notes and are never generated, because a safe primary key migration needs a table rebuild on most engines.

## Generating and running DDL directly

```python
User.create_table_sql()   # dialect-specific CREATE TABLE
User.create_table(conn)   # execute it
User.drop_table(conn)     # DROP TABLE IF EXISTS
```

`create_table()` and `drop_table()` commit when they finish, the same way `run()` does, so the DDL survives the connection closing. Inside a `transaction()` block they leave the commit to the block.

Introspection reads one schema: the one the connection is on, plus any schema a model names in `tableSchema`. A snapshot keys its tables on the bare name, so two models declaring the same table name in different schemas are refused; diff them in separate calls. See [Schema scope](./reference/dialects#schema-scope).

Types map per dialect: `BIT`, `NVARCHAR`, and `DATETIME2` on MSSQL; `JSONB` and identity columns on Postgres; plain `INTEGER PRIMARY KEY` rowid behavior on the default dialect. Schema introspection reads SQLite's PRAGMA tables on the default dialect, and `information_schema.columns` elsewhere.

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

- **Diffs read the connection's schema only.** Introspection scopes to `current_schema`, so a plan sees the tables in the schema the connection was opened on and nothing from other Glue databases.
- **Every string column renders `STRING`.** `String(n)` and `Text()` are the same column type on Athena. Iceberg tables reject `VARCHAR`, and Athena enforces no length either way, so the declared length only documents intent. The engine reports the column back as `varchar`, which the diff reads as the same type.
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

- **Column changes use Iceberg rules.** A generated migration adds columns with `ALTER TABLE ... ADD COLUMNS` and changes types with `CHANGE COLUMN`, which Iceberg only allows for widenings such as `INT` to `BIGINT`. Renames, nullability changes, and `type_casts` hints raise `DialectError`; write those by hand in a `Migration`.

Upserts (`onConflict().merge()`), `UPDATE`, `DELETE`, and `down()` reverts all depend on Iceberg tables. Plain Hive external tables are read-and-append only.

## MySQL and MariaDB

`Dialects.MYSQL` serves both. Everything above works there, with one difference that changes the workflow: MySQL has no transactional DDL. Every schema statement commits the moment it runs, whatever the surrounding transaction does.

That means `migrate` does not wrap a migration in a transaction, because there is nothing a rollback would take back. A migration that fails halfway leaves the statements before it applied. The run records a failure row against that migration, so recovery is the one already described under [Validation and repair](#validation-and-repair): read what landed, finish or undo it by hand, then run `sustained repair` to clear the row. `sustained script up` prints the statements the run would have executed, in order, which is how you find where it stopped.

It also means `rehearse` refuses MySQL against the real database, and asks for [a scratch one](#rehearsing-on-a-scratch-database) instead.

[SQL Dialects](./dialects#mysql-and-mariadb) has the type mapping, the MariaDB divergences, and the column shapes MySQL will not accept.

## Hand-written migrations

Anything autogeneration will not express, you write explicitly. A `Migration` pairs an id with an up step and an optional down step. A step is a SQL string, a list of statements, a [typed ddl step](#typed-migration-steps), or a callable receiving the connection. Hand-written and generated migrations share one ordered list and one tracking table.

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

### Typed migration steps

A step can also be a typed `ddl` step instead of a SQL string. Each step names one schema change and renders through the dialect compiler when the migration runs, or when `script()` prints it, so one hand-written migration serves every dialect:

```python
from sustained import ddl
from sustained.migrations import Migration
from sustained.schema import Index, String

migrations = [
    Migration('add_nickname', up=[
        ddl.add_column('users', 'nickname', String(60)),
        ddl.create_index('users', Index('ix_users_nickname', 'nickname')),
    ]),
]
```

The available steps are `create_table`, `drop_table`, `add_column`, `drop_column`, `rename_column`, `rename_table`, `add_foreign_key`, `drop_foreign_key`, `add_check`, `drop_constraint`, `create_index`, `drop_index`, `create_enum`, `drop_enum`, `add_enum_value`, and `sql()` for one raw statement. The [migrations reference](./reference/migrations#typed-ddl-steps) has every signature.

The migration above declares no down step, and it does not need one: a step that creates, adds, or renames something knows the step that takes it back, so a migration whose up is all reversible ddl steps derives its down automatically, the inverses newest first. The example's derived down drops the index, then drops the column. A step that cannot reverse, which is any drop, `add_enum_value`, or `sql()`, refuses the derivation with `ValueError`; give that migration an explicit down step, or `down=None` to declare it irreversible. An explicit down step always wins over the derivation.

Guards, the `destructive` labels, and the rehearsal gate all read a ddl step's rendered SQL, which none of them can do for a callable step. When a change fits the typed steps, prefer them over a callable for that reason.

The checksum of a ddl step hashes its operation name and arguments rather than its rendered SQL, so it is the same on every dialect, and moving a project from SQLite to Postgres never invalidates an applied migration. One caveat: `ddl.create_table(model)` reads the model's columns when the step is built, so a later model edit changes the migration's checksum. Pass an explicit `columns` mapping when the migration must outlive the model.

## Migrations as SQL files

Migrations can live as plain SQL files instead of Python objects. `load_migrations()` reads a directory of `<id>.up.sql` files, each optionally paired with a `<id>.down.sql`, and returns `Migration` objects ordered by id, so file naming fixes the apply order. Statements split at line-ending semicolons, with or without a `--` comment after the semicolon; semicolons inside string literals stay intact.

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

Empty up or down files, a down file without its up file, and files that fit no naming pattern raise `ValueError`. The naming check reads every file in the directory, whatever its extension, so a typo like `0002_add.up.sq` cannot be skipped silently. Keep other files, a README included, outside the migrations directory. Subdirectories are passed over, and so are dotfiles and editor backup files (`*~`, `*.bak`, `*.orig`, `*.swp`, `*.swo`, `*.tmp`).

## Repeatable migrations

Views, functions, and seed data are replaced rather than evolved, so they fit badly in versioned migrations. A `<id>.repeat.sql` file is a repeatable migration: it runs whenever its checksum is new or changed, after every versioned migration. A targeted `migrate` skips repeatables, because one may depend on a versioned migration past the target; the next full `migrate` runs them.

```sql
-- active_users.repeat.sql
DROP VIEW IF EXISTS active_users;
CREATE VIEW active_users AS SELECT * FROM users WHERE active = 1;
```

Edit the file and the next `migrate` re-runs it, so keep the SQL replace-safe (`CREATE OR REPLACE`, or a drop first). The tracking table holds one row per repeatable, updated in place on re-runs with its original sequence number. Repeatables have no down file, and `down` never touches them. `baseline` records every repeatable at its current checksum, so adopting an existing schema does not re-run objects it already holds.

`status` (and `Migrator.statuses()`) reports each migration as `applied`, `pending`, or `changed`. `changed` marks a repeatable whose contents differ from its last run. In Python, pass `Migration(id, up=..., repeatable=True)`. A repeatable with a callable step needs an explicit `checksum` so that re-runs can be detected. An id may not have both an up file and a repeat file.

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

Substitution happens before checksums compute, so the checksum covers the SQL that actually ran. Changing a placeholder value after a versioned migration applied flags a checksum mismatch, because different SQL was applied; run `repair()` if the new value is intentional. A repeatable needs no repair: its changed checksum re-runs it with the new value.

## Migrations without a transaction

The migrator wraps each migration in a transaction, on the engines that can roll a schema change back. Some statements refuse to run there. `CREATE INDEX CONCURRENTLY` on Postgres is the common one: it is the safe way to build an index on a busy table, and Postgres rejects it inside a transaction block.

Set `transactional=False` on the migration and it runs outside a transaction:

```python
Migration(
    '005_orders_index',
    up='CREATE INDEX CONCURRENTLY orders_customer_idx ON orders (customer_id)',
    down='DROP INDEX CONCURRENTLY orders_customer_idx',
    transactional=False,
)
```

A SQL file asks for the same thing with a marker comment on a line of its own:

```sql
-- 005_orders_index.up.sql
-- sustained: no transaction
CREATE INDEX CONCURRENTLY orders_customer_idx ON orders (customer_id);
```

The marker is read from the up file and the repeat file. Case does not matter. The flag belongs to the migration, so it covers the down step too, and a marker in a down file changes nothing.

The migrator turns the driver's own transaction control off for the migration and turns it back on after, because a DB-API driver such as psycopg2 opens a transaction before your first statement whether you asked for one or not. The tracking row is written after the statements, in the same mode, so a migration that finishes is still recorded.

Nothing rolls a failed one back. The statements that already ran stay in the database, and the tracking row records a failed attempt, so validation stops the next `migrate` until you clean up and run `sustained repair`. Read what landed, finish or undo the rest by hand, then repair. A failed `CREATE INDEX CONCURRENTLY` also leaves an invalid index behind, which you must drop before you try again. Keep such a migration to one statement and the cleanup stays small.

`AsyncMigrator` reads the same flag and runs the migration bare, with one limit: an adapter over a driver that opens its own transaction, such as `DbApiAsyncAdapter` over psycopg2, still opens one, and a statement that refuses a transaction block still fails there. Run it on `AsyncpgAdapter`, which executes every statement bare.

## Command line

The `sustained` console script (also `python -m sustained`) runs migrations from the shell. It imports a config module, `sustained_config` by default or `--config mymodule`, from the current directory:

```python
# sustained_config.py
import sqlite3

def get_connection():
    return sqlite3.connect('app.db')

migrations_dir = 'migrations'
# optional: migrations = [...], placeholders = {...},
# models = [User, Post], dialect = 'postgres', table = '...',
# rehearsal_table = '...', tracking_table_options = TableOptions(...),
# guards = [no_drops()], get_rehearsal_connection(),
# before_migrate(), after_migrate(), on_error()
```

```console
$ sustained plan                    # what a run would do; exits 2 when work is waiting
$ sustained status
$ sustained rehearse                # run it all, forwards and back, then roll back
$ sustained migrate                 # --target ID, --no-validate, --allow-out-of-order, --unrehearsed
$ sustained down                    # --steps N (0 or more) or --to ID, --allow-changed
$ sustained validate                # exits 1 when problems exist
$ sustained repair
$ sustained script down             # print the SQL without running it
$ sustained baseline 001_create_users
```

Commands exit 0 on success and 1 on failure, with errors on stderr, so they slot into deploy pipelines. `plan` exits 2 when work is waiting, `plan` and `migrate` exit 3 when a [guard](#guards) blocked a statement, and `migrate` exits 4 when a run that removes data has no rehearsal row.

When the config module names `models`, `rehearse` and `migrate` use them: `rehearse` proves the generated migration alongside the pending ones, and `migrate` applies it after them. A targeted `migrate` applies the registered migrations only, since the generated migration always runs last.

After a successful run, `migrate` reads the schema back and says what it found:

```console
$ sustained migrate
applied  003_sessions
applied  auto_20260816120000_001
schema matches the models
```

A difference prints one `drift` line each. This is a report only: the run has already happened, and the exit code does not change.

### Reading a plan

`sustained plan` shows what a run would do, in one screen. It merges three sources: the migrations waiting to run, the problems `validate` would report, and the gap between the config module's `models` and the database.

```console
$ sustained plan
pending
  003_sessions  2 statements
  004_trim      1 statement
    destructive  ALTER TABLE users DROP COLUMN legacy
  vw_active     1 statement  repeat changed

drift
  ALTER TABLE users ADD COLUMN bio TEXT

2 pending migrations, 1 drift statement
run: sustained rehearse
```

A statement that removes data or an object that holds it is labelled `destructive`: `DROP TABLE`, `DROP COLUMN`, `DROP TYPE`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, `DROP SCHEMA ... CASCADE`, a constraint drop, `TRUNCATE`, and `DELETE FROM`. A column drop written without the COLUMN keyword, as MySQL allows, is labelled as well. A plain `DROP SCHEMA` refuses a schema that holds anything, so only the CASCADE form is labelled. The scan is textual, but it keeps comments and quoted text out, so a drop named inside a string literal is not labelled.

The footer points at `rehearse` rather than `migrate` when a pending migration carries one of these labels, because `migrate` will refuse it until a rehearsal has proved it. Once a rehearsal has recorded a row for that run, the footer reads `run: sustained migrate` again. With nothing destructive waiting, it reads `run: sustained migrate` from the start.

The drift section appears only when the config module names `models`. It reports every difference, drops included, while `migrate` never generates a drop. When drops are all that is left, the footer says so instead of offering `sustained migrate`. With no `models`, the plan says drift went unchecked rather than reporting none.

When the config module names `guards`, `plan` runs them and prints a fourth section. See [Guards](#guards). The guards read the statements `migrate` would apply: the pending migrations, and the generated migration without the drops. The drift section is the wider set, so a drop it lists carries no verdict, because no run would read it.

`plan` exits 0 when the database is current, 2 when work is waiting, 3 when a guard blocked a statement, and 1 when validation found problems. Validation problems take priority over the other codes, and a blocked statement takes priority over work that is merely waiting. Note that argparse also exits 2 on a usage error, so a script that treats 2 as "work is waiting" should check stderr for an `error:` line.

### Machine-readable output

`status`, `validate`, `plan`, and `rehearse` take `--json`, which prints one JSON object to stdout instead of the plain lines. Exit codes are the same either way.

```console
$ sustained plan --json
{
  "pending": [
    {
      "id": "004_trim",
      "state": "pending",
      "repeatable": false,
      "statements": [
        {
          "sql": "ALTER TABLE users DROP COLUMN legacy",
          "destructive": true,
          "guards": [{"rule": "no_drops", "verdict": "block"}]
        }
      ],
      "destructive": ["ALTER TABLE users DROP COLUMN legacy"]
    }
  ],
  "problems": [],
  "drift": null
}
```

Every command that reports SQL uses that statement object, `drift` included. `statements` is `null` for a callable step, which renders no SQL. A guard verdict rides on the statement it flags and appears nowhere else, so there is one place to read what a statement will do. Statements no rule flagged carry an empty `guards` list. `drift` is `null`, not `[]`, when the config module names no models, so a caller can tell "nothing was compared" from "compared and found no gap". `status --json` prints `{"migrations": [{"id": ..., "state": ...}]}` and `validate --json` prints `{"ok": ..., "problems": [...]}`. Output is plain in both modes; nothing is coloured.

Before version 2.13.0, `statements` was a count. A script that read the number needs updating.

## Rehearsing a migration

Where `plan` only reads the migrations, `rehearse` runs them. It applies every pending migration, runs the down steps back down, and rolls the whole thing back, so the database ends where it started and you learn whether the SQL is valid and whether it reverses.

```console
$ sustained rehearse
rehearsed 003_sessions             up ok, down ok, reversed
rehearsed 004_trim                 up ok, down ok, reversed
rehearsed auto_20260816120000_001  up ok, landed, down ok, reversed
rehearsed vw_active                up ok, no down step (repeatable)
rollback complete, database unchanged
```

The words after the id are what the rehearsal proved, in the order it proved them.

- `up ok`: the up statements ran.
- `landed`: the schema then matched the models. Only the generated migration carries this, and only when the config module names `models`. A hand-written migration may create objects no model declares, so comparing it against the models would fail runs that are correct.
- `down ok`: the down statements ran.
- `reversed`: the schema after the down sweep matched a snapshot taken before the run.

A check that fails says `not landed` or `not reversed` and lists what is wrong:

```console
$ sustained rehearse
rehearsed 004_trim  up ok, down ok, not reversed
    leftover     table 'users_audit' left behind
rollback complete, database unchanged
run: sustained plan
```

A broken migration names the statement that failed and the migrations under it that never got their turn:

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down not rehearsed: the run stopped
failed    004_trim      up: column "legacy" of relation "users" does not exist
rollback complete, database unchanged
run: sustained plan
```

The run exits 1 when an up or a down step failed, when the models did not land, or when the schema did not come back, and 0 otherwise. A migration with no down step is not a failure: the line says `no down step`, and the migrations older than it report `down not reached`, because they sit under changes that cannot be taken back. Repeatables run in their usual place, after the versioned migrations, and have no down step to prove.

A rehearsal proves that the statements are valid, that the models arrive, and that the down steps take the schema back. It proves nothing about how long they take on a production-sized table, or what happens to the rows in it.

The `reversed` check compares tables and columns. Indexes, constraints, and column defaults are left out, because engines report those in spellings that differ between an original object and a rebuilt one, and the check would report differences that are not real. A leftover index after a down step is not detected yet. Where the schema cannot be read at all, the check reports as not run instead of failing.

`sustained rehearse` passes the config module's `models` when it names any, so the generated migration is rehearsed with the pending ones. It is never registered, and it rolls back with everything else.

`rehearse --json` prints one object: `{"rehearsed": [...], "scratch": false, "key": "...", "recorded": true, "ok": true}`. In each result, `landed` and `reversed` are `null` when the check did not run, `[]` when it passed, and the lines naming the trouble when it failed. `key` and `recorded` describe the rehearsal row, covered next.

The rehearsal creates the tracking table when the database has none, because it reads the applied rows before it opens its transaction. It also creates the rehearsal table and writes one row there after the rollback, described below. Nothing else survives: the tracking rows the rehearsal writes roll back with everything else, and the migrations stay pending. A callable step that commits on its own is the exception, since that commit cannot be taken back.

Only databases whose schema changes roll back can rehearse: SQLite, Postgres, and DuckDB. The rest are refused, and so is a connection in autocommit mode or one inside an open `transaction()` block, because none of them could take the changes back. The check reads the declared dialect, so declare it. The default dialect passes, since the generic compiler usually serves SQLite. A config that leaves the dialect unset while pointing at MySQL, whose DDL commits as it runs, would rehearse for real.

### Rehearsal logging and tracking

A rehearsal that passes writes one row into a second table, `sustained_rehearsals`, created on first use like the tracking table. The row holds a key, whether the run passed, and when it ran.

`migrate` reads it. A run that would remove data stops unless a passing rehearsal row covers it. It reads the same list the `destructive` labels use, `DELETE FROM` included:

```console
$ sustained migrate
error: This run removes data, and no rehearsal has proved these statements:
  004_trim  ALTER TABLE users DROP COLUMN legacy
Prove them first: sustained rehearse
Or apply them without proof: sustained migrate --unrehearsed
```

Rehearse, and the same command goes through:

```console
$ sustained rehearse
rehearsed 004_trim  up ok, down ok, reversed
rollback complete, database unchanged
rehearsal row recorded

$ sustained migrate
applied  004_trim
```

A run that only adds is never gated and never reads the table.

The key is a SHA-256 over two ordered lists: the checksums of the migrations already applied, and the checksums of the migrations about to run. A rehearsal row therefore proves the content of a run rather than its ids.

- **Editing a migration voids its rehearsal row.** The statements changed, so the key changed, and the gate closes again.
- **A different history voids it too.** A rehearsal proves a set of statements against one starting schema. A database that has applied a different set of migrations gets its own key and its own rehearsal.

Ids are not part of the key, only statements. A generated migration takes a new timestamped id every time the diff runs, and its rehearsal row survives that. A model edit that leaves the generated SQL unchanged keeps the row too.

`migrate --target` runs a shorter set, which has its own key. A rehearsal applied every one of those shorter sets on its way up and took them all back on the way down, so it records a row for each one that removes data. The rows cover every start point too, because a targeted run leaves a history the next targeted run starts from. One rehearsal therefore covers the whole run, every target within it, and a sequence of targeted runs that walks through it.

`--unrehearsed` is the override. It writes a row of its own under the same key, with the outcome `override`, so the database records what was applied unproved and when. That row never opens the gate for a later run: only a `passed` row does. There is no config setting that turns the gate off.

A refused run exits 4, which a pipeline can tell apart from a failure. A targeted run gets the target back in the suggested command, so the line can be copied as it stands.

There are two limits here, both shared with the `destructive` labels in `plan`:

- A callable step has no SQL to read, so it never triggers the gate. A callable that drops a table applies without a rehearsal row.
- The scan is textual. It reads the words in a statement, not its structure. A `DELETE FROM` that removes one row gates the run the same way as one that removes every row.

In Python the same rules apply through the API. `rehearse()` returns a `Rehearsal`, which is a list of results carrying `key`, `recorded`, and `ok`:

```python
rehearsal = migrator.rehearse()
if rehearsal.ok:
    migrator.up()                              # the rehearsal row opens the gate
migrator.up(unrehearsed=True)                  # or skip the proof

migrator.rehearsed(rehearsal.key)              # True
migrator.rehearsal_outcome(rehearsal.key)      # 'passed', 'failed', or None
migrator.record_rehearsal(rehearsal.key)       # write one by hand
```

A failing rehearsal records its failure under the same key, so the refusal reads `The last rehearsal of these statements failed` rather than reporting no rehearsal at all.

`AsyncMigrator` has the same methods, the same flag on `up()`, and the same key function, so a row written by either migrator opens the gate for the other on the same database. Both migrators cover a generated migration the same way, so a rehearsal run by one opens the gate for the other.

### Rehearsing on a scratch database

Where the rollback cannot be trusted, point the rehearsal somewhere disposable. A config module that defines `get_rehearsal_connection()` sends `rehearse` there instead:

```python
def get_rehearsal_connection():
    return psycopg.connect('postgresql://localhost/app_rehearsal')
```

The scratch database is usually empty, so the whole history replays rather than what is pending on the real one, which proves the migrations run from nothing. The dialect check does not apply, the changes may survive the rollback, and the footer says so. The connection closes when the command ends. On an engine whose schema changes do not roll back, the rehearsed objects stay behind, so recreate the scratch database before the next rehearsal.

The rehearsal row belongs on the database `migrate` will read, not on the throwaway one, so the CLI writes it there after the scratch run passes. The key is computed against the real database's history and pending set. It is written only when the scratch run applied every migration pending on the real database; otherwise the output says the row was not recorded. Rows go in for the shorter target sets too, the same ones a real rehearsal records, so `migrate --target` reads a row after a scratch run. A scratch rehearsal cannot cover a generated migration, because the diff it runs against the throwaway schema is not the diff the real run will produce.

Through the API, `rehearse(scratch=True)` writes nothing at all. Take the key off the result and record it yourself on a migrator bound to the real database:

```python
rehearsal = scratch_migrator.rehearse(scratch=True)
if rehearsal.ok:
    real_migrator.record_rehearsal(rehearsal_key(
        real_migrator.applied_records(), real_migrator.pending()
    ))
```

In Python, `migrator.rehearse()` returns a `Rehearsal`: a list of `RehearsalResult(id, up_ok, down_ok, error, landed, reversed)` with `key`, `recorded`, and `ok` on it. `down_ok` is `None` when nothing was proved, and `error` then says why. `landed` and `reversed` follow the same rule as the JSON output: `None` not checked, `[]` proved, a list of lines when it failed. Pass `rehearse(models=[User, Show])` to rehearse the model diff too, and `rehearse(scratch=True)` for a connection to a database you can throw away. `AsyncMigrator.rehearse()` is the same on an adapter, `models` included.

## Guards

A rehearsal proves that a migration works. A guard decides whether it should run at all. Guards are your team's rules about SQL: no drops in a deploy, every index built concurrently, no run longer than fifty statements.

Give them to the migrator, or name them in the config module:

```python
from sustained.guards import index_must_be_concurrent, max_statements, no_drops

guards = [no_drops(), index_must_be_concurrent(), max_statements(50)]

migrator = Migrator(conn, migrations, dialect=Dialects.POSTGRES, guards=guards)
```

Each rule returns a verdict on each statement it objects to: `block` or `warn`. A `block` raises `GuardBlocked`. A `warn` prints on stderr and the run goes on. A block on the registered migrations stops the run before a single statement runs. A block on the migration generated from your models comes later, because those statements do not exist until the registered migrations have applied; the registered ids print before the error, and they stay applied.

```console
$ sustained plan
pending
  003_sessions  2 statements
  004_trim      1 statement
    destructive  ALTER TABLE users DROP COLUMN legacy

guards
  block  no_drops          ALTER TABLE users DROP COLUMN legacy
  warn   no_table_rewrite  ALTER TABLE users ALTER COLUMN age TYPE BIGINT

2 pending migrations, 2 guard verdicts
blocked: fix the statement, or take the rule out of guards

$ sustained migrate
error: A guard blocked this run:
  no_drops  ALTER TABLE users DROP COLUMN legacy
Fix the statement, or take the rule out of the guard list to run it anyway.
```

Both commands exit 3. There is no `--force` flag: fix the statement, or take the rule out of the list.

### The rules

| Rule | Verdict | Flags |
| --- | --- | --- |
| `no_drops()` | block | A statement that drops a table, column, view, materialized view, schema, database, enum type, or constraint |
| `index_must_be_concurrent()` | block | `CREATE INDEX` without `CONCURRENTLY`, on Postgres only |
| `no_table_rewrite()` | warn | A column type change, or a NOT NULL with nothing to fill existing rows |
| `no_lock_without_timeout()` | block | A statement that alters or drops a table with no `SET lock_timeout` still in force before it, on Postgres only |
| `max_statements(n)` | block | Every statement past the limit |

Every one is a factory, so they all read the same at the call site. `no_table_rewrite()` warns where the others block, because whether a change rewrites the table depends on the engine, its version, and whether the two types coerce. Read it against your own engine rather than trusting it.

`index_must_be_concurrent()` and `no_lock_without_timeout()` are silent on every dialect but Postgres, the only one with the keyword and the setting they are about.

`CONCURRENTLY` needs a migration of its own with `transactional=False`, because Postgres refuses that form inside a transaction block. See [Migrations without a transaction](#migrations-without-a-transaction).

### What guards read, and when

Guards read every SQL statement an up run would apply: SQL file migrations, Python migrations with string steps, and the diff against your models. A callable step renders no SQL, so guards cannot see inside it, the same limit the destructive labels carry.

Down runs are not checked. A down undoes work the rules already passed, so `no_drops()` would block every rollback of a create.

A rule reads the run in order, and each statement it reads names the migration it came from. That matters for `no_lock_without_timeout()`. A plain `SET lock_timeout`, with or without SESSION, sets the timeout for the session, so it covers every statement after it in the run. A `SET LOCAL lock_timeout` dies at the commit that ends its migration, so it covers only the statements after it in that same migration, and the next migration starts uncovered. In a migration with `transactional=False` there is no transaction block for a LOCAL setting to live in, so Postgres ignores it and the rule counts it for nothing; write the plain `SET lock_timeout` there.

The statements a guard receives are strings, so a rule written as a function over strings needs no change to read them.

`migrate` checks twice. The registered migrations are checked before anything runs. The diff against the models cannot be generated until those have run, so its statements are checked the moment they exist, alongside the registered statements from the same run, so a rule about the whole run counts the whole run. A warning already printed is not printed again.

`rehearse` does not enforce guards. It runs against a database it is about to roll back, and stopping it there would stop you from testing the statement you are trying to fix.

Writing your own rule takes a function of two arguments:

```python
from sustained.guards import BLOCK, Verdict

def no_seed_data():
    def guard(statements, dialect):
        return [
            Verdict('no_seed_data', BLOCK, s)
            for s in statements
            if s.upper().startswith('INSERT')
        ]
    return guard
```

There is no rule language, and no severity beyond the two verdicts. A guard is a plain Python function.

## Callbacks around a run

The migrator calls these functions around `up()`, whichever ones you give it:

```python
from sustained.migrations import Callbacks, Migrator

migrator = Migrator(conn, migrations, callbacks=Callbacks(
    before_migrate=lambda connection: notify('migration starting'),
    after_migrate=lambda connection, applied: notify(f'applied {len(applied)}'),
    on_error=lambda connection, migration_id, error: page_someone(
        f'{migration_id} failed: {error}'
    ),
))
```

From the shell, the config module names them as plain functions and the CLI collects them:

```python
def before_migrate(connection):
    notify('migration starting')

def after_migrate(connection, applied):
    notify(f'applied {len(applied)} migrations')

def on_error(connection, migration_id, error):
    page_someone(f'{migration_id} failed: {error}')
```

`before_migrate` runs before the run starts, which is before validation and before the advisory lock. `after_migrate` runs only when at least one migration applied, so a run with nothing to do stays quiet. `on_error` runs after the failure and before it reaches the caller. If the callback itself raises, its error prints on stderr and the migration error is the one that propagates. `migration_id` names the migration that failed, or is `None` when the run failed before reaching one, which is what a guard block or a validation problem looks like.

Only `up()` calls them. `rehearse` does not, since nothing real happened. `AsyncMigrator` takes the same `Callbacks`, receives the adapter as the first argument, and awaits a callback that returns an awaitable, so `async def before_migrate(adapter)` works.

## Offline review and async

`migrator.script('up')` renders every statement a run would execute, including tracking bookkeeping, without touching the database, for review or DBA handoff. `script('down')` renders the rollback. Neither writes anything, not even the tracking table: a database without one renders as a database with no migrations applied. `status()`, `statuses()`, `pending()`, and `validate()` read the same way, and `read_applied_records()` gives you those rows directly. For async services, `AsyncMigrator` in `sustained.aio_migrations` runs the same `Migration` objects on an `AsyncAdapter` with the same `up`, `down`, `down_to`, `status`, `statuses`, `validate`, `repair`, `baseline`, and `script` surface; callable steps receive the adapter and are awaited. `await migrator.script('up')` renders the same text, and reads the rows through `read_applied_records()` the same way.
