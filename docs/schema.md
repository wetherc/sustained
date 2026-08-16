---
layout: default
title: Schema and Migrations
---

Sustained manages your database schema from your models. Declare typed columns once, then let the migrator create tables, detect drift, generate migrations, rehearse them, apply them, and roll them back.

A schema change is the one thing an application does that a retry cannot undo, so this page is the longest in the documentation. It covers the whole path a migration takes: generated or hand-written, planned, rehearsed, applied under a lock, checksummed in a tracking table, and reverted.

## Automated Migration in One Call

`Migrator.up(models=[...])` diffs the live database against your models, generates the migration, records it, and applies it with everything else pending. Run it again after changing a model and only the difference is applied. Nothing to hand-write for additive changes.

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

The diff is taken after the pending migrations have run, so it sees the schema they left. A generated migration always runs last of the versioned ones, which is why `models` cannot be combined with a `target`.

`Migrator.sync()` did this before version 2.13.0. It still works, warns, and goes away in 3.0.

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

`repair()` brings the tracking table back in line: it deletes rows left by failed attempts and rewrites stored checksums after an intentional edit, including null checksums on rows written before checksums existed. Repair only fixes bookkeeping; schema changes a failed attempt left behind need manual cleanup first. Repeatables keep their stored checksums: a changed repeatable is scheduled to re-run, and the next `up()` runs it.

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

`migrator.plan()` returns the migration `up(models=[...])` would generate, without registering or applying it, so its statements can be reviewed first. It returns `None` when the schema is current and takes the same diff options.

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

- **Drops are opt-in.** Extra tables and columns are left alone unless `allow_drops=True`, which generates the drops. A migration containing drops has no down step, because the dropped data cannot come back. `autogenerate()` called directly still raises on undeclared objects; the migrator passes `ignore_undeclared=True`, since a database that hand-written migrations also touch holds tables no model declares.
- **Type and nullability changes migrate per dialect.** Postgres, MSSQL, and DuckDB alter in place with reversible down steps; Postgres casts take a hint through `type_casts={'table.col': 'col::integer'}`. SQLite rebuilds the table (create new, copy rows, replace), which is not reversible. Pass `ignore_changed_columns=True` to skip them entirely.
- **NOT NULL needs a value for existing rows.** Adding or tightening to NOT NULL requires a `default` or a `backfill` value on the ColumnDef; generation emits add-nullable, UPDATE backfill, SET NOT NULL, or folds the backfill into a SQLite rebuild. New primary key or autoincrement columns cannot be added with ALTER TABLE.
- **Statements that remove data need a rehearsal.** `up()` refuses to run a DROP, a column drop, or a TRUNCATE until a passing rehearsal has proved that exact set of statements. `up(unrehearsed=True)` applies them anyway. See [The receipt a rehearsal leaves](#the-receipt-a-rehearsal-leaves).
- **Your own rules run too.** Guards read every statement a run would apply, generated or hand-written, and can block it. See [Guards](#guards).
- The tracking table and the receipt table are excluded from diffing, and `exclude_tables` protects any other tables Sustained does not manage.

Renames cannot be detected from the catalog, so pass hints: `up(models=models, renames={'users.name': 'full_name'}, table_renames={'old': 'new'})` emits reversible RENAME statements instead of a destructive drop-plus-add.

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

- **Column changes use Iceberg rules.** A generated migration adds columns with `ALTER TABLE ... ADD COLUMNS` and changes types with `CHANGE COLUMN`, which Iceberg only allows for widenings such as `INT` to `BIGINT`. Renames, nullability changes, and `type_casts` hints raise; write those by hand in a `Migration`.

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

Views, functions, and seed data are replaced rather than evolved, so they fit badly in versioned migrations. A `<id>.repeat.sql` file is a repeatable migration: it runs whenever its checksum is new or changed, after every versioned migration. A targeted `migrate` skips repeatables, because one may depend on a versioned migration past the target; the next full `migrate` runs them.

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

Substitution happens before checksums compute, so the checksum covers the SQL that actually ran. Changing a placeholder value after a versioned migration applied flags a checksum mismatch, because different SQL was applied; run `repair()` if the new value is intentional. A repeatable needs no repair: its changed checksum re-runs it with the new value.

## Command Line

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
$ sustained down                    # --steps N or --to ID
$ sustained validate                # exits 1 when problems exist
$ sustained repair
$ sustained script down             # print the SQL without running it
$ sustained baseline 001_create_users
```

Commands exit 0 on success and 1 on failure, with errors on stderr, so they slot into deploy pipelines. `plan` exits 2 when work is waiting, and `plan` and `migrate` exit 3 when a [guard](#guards) blocked a statement.

When the config module names `models`, `rehearse` and `migrate` use them: `rehearse` proves the generated migration alongside the pending ones, and `migrate` applies it after them. A targeted `migrate` applies the registered migrations only, since the generated migration always runs last.

After a successful run, `migrate` reads the schema back and says what it found:

```console
$ sustained migrate
applied  003_sessions
applied  auto_20260816120000_001
schema matches the models
```

A difference prints one `drift` line each. It is a report, not a gate: the run has already happened, and nothing about the exit code changes.

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

A statement that drops a table, drops a column, or truncates one is labelled `destructive`. A column drop written without the COLUMN keyword, as MySQL allows, is labelled too. The scan is textual, so a drop named inside a string literal is labelled too.

The footer points at `rehearse` rather than `migrate` when a pending migration carries one of these labels, because `migrate` will refuse it until a rehearsal has proved it. With nothing destructive waiting, the footer reads `run: sustained migrate`.

The drift section appears only when the config module names `models`. It reports every difference, drops included, while `migrate` never generates a drop. When drops are all that is left, the footer says so instead of offering `sustained migrate`. With no `models`, the plan says drift went unchecked rather than reporting none.

When the config module names `guards`, `plan` runs them over every statement it just listed and prints a fourth section. See [Guards](#guards).

`plan` exits 0 when the database is current, 2 when work is waiting, 3 when a guard blocked a statement, and 1 when validation found problems. Problems win over everything: a plan that cannot be trusted is worse news than a statement that will be refused. A blocked statement in turn wins over work merely waiting. Note that argparse also exits 2 on a usage error, so a script that treats 2 as "work is waiting" should check stderr for an `error:` line.

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

## Rehearsing a Migration

`plan` reads the migrations. `rehearse` runs them. It applies every pending migration, runs the down steps back down, and rolls the whole thing back, so the database ends where it started and you learn whether the SQL is valid and whether it reverses.

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
- `landed`: the schema then matched the models. Only the generated migration carries this, and only when the config module names `models`. A hand-written migration may create objects no model declares, so comparing it against the models would fail honest runs.
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

The `reversed` check compares tables and columns. Indexes, constraints, and column defaults are left out: engines report those in spellings that differ between an original object and a rebuilt one, and a check we cannot stand behind is worse than no check. A leftover index after a down step is not detected yet. Where the schema cannot be read at all, the check reports as not run instead of failing.

`sustained rehearse` passes the config module's `models` when it names any, so the generated migration is rehearsed with the pending ones. It is never registered, and it rolls back with everything else.

`rehearse --json` prints one object: `{"rehearsed": [...], "scratch": false, "key": "...", "recorded": true, "ok": true}`. In each result, `landed` and `reversed` are `null` when the check did not run, `[]` when it passed, and the lines naming the trouble when it failed. `key` and `recorded` are the receipt, described next.

The rehearsal creates the tracking table when the database has none, because it reads the applied rows before it opens its transaction. It also creates the receipt table and writes one row there after the rollback, described below. Nothing else survives: the tracking rows the rehearsal writes roll back with everything else, and the migrations stay pending. A callable step that commits on its own is the exception, since that commit cannot be taken back.

Only databases whose schema changes roll back can rehearse: SQLite, Postgres, and DuckDB. The rest are refused, and so is a connection in autocommit mode or one inside an open `transaction()` block, because none of them could take the changes back. The check reads the declared dialect. The default dialect passes it, since the generic compiler usually serves SQLite; a config that leaves the dialect unset while pointing at an engine like MySQL would rehearse for real, so declare the dialect or use a scratch database.

### The receipt a rehearsal leaves

A rehearsal that passes writes one row into a second table, `sustained_rehearsals`, created on first use like the tracking table. The row is the receipt: a key, whether the run passed, and when.

`migrate` reads it. A run that would drop a table, drop a column, or truncate one stops unless a passing receipt covers it:

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
receipt recorded

$ sustained migrate
applied  004_trim
```

A run that only adds is never gated and never reads the table.

The key is a SHA-256 over two ordered lists: the checksums of the migrations already applied, and the checksums of the migrations about to run. So a receipt proves content, not names.

- **Editing a migration voids its receipt.** The statements changed, so the key changed, and the gate closes again.
- **A different history voids it too.** A rehearsal proves a set of statements against one starting schema. A database that has applied a different set of migrations gets its own key and its own rehearsal.

Ids are not part of the key, only statements. A generated migration takes a new timestamped id every time the diff runs, and its receipt survives that. A model edit that leaves the generated SQL unchanged keeps the receipt too.

`migrate --target` runs a shorter set, which has its own key. A rehearsal applied every one of those shorter sets on its way up and took them all back on the way down, so it records a receipt for each one that removes data. One rehearsal covers the whole run and every target within it.

`--unrehearsed` is the override, named so a shell history records what was done. There is no config setting that turns the gate off.

Two limits, both shared with the `destructive` labels in `plan`:

- A callable step has no SQL to read, so it never triggers the gate. A callable that drops a table applies without a receipt.
- The scan is textual. `DROP` inside a string literal counts.

In Python the same rules apply through the API. `rehearse()` returns a `Rehearsal`, which is a list of results carrying `key`, `recorded`, and `ok`:

```python
rehearsal = migrator.rehearse()
if rehearsal.ok:
    migrator.up()                              # the receipt opens the gate
migrator.up(unrehearsed=True)                  # or skip the proof

migrator.rehearsed(rehearsal.key)              # True
migrator.rehearsal_outcome(rehearsal.key)      # 'passed', 'failed', or None
migrator.record_rehearsal(rehearsal.key)       # write one by hand
```

A failing rehearsal records its failure under the same key, so the refusal reads `The last rehearsal of these statements failed` rather than reporting no rehearsal at all.

`AsyncMigrator` has the same three methods, the same flag on `up()`, and the same key function, so a receipt written by either migrator opens the gate for the other on the same database. The async rehearsal covers registered migrations only, so its receipt never includes a generated migration.

### Rehearsing on a scratch database

Where the rollback cannot be trusted, point the rehearsal somewhere disposable. A config module that defines `get_rehearsal_connection()` sends `rehearse` there instead:

```python
def get_rehearsal_connection():
    return psycopg.connect('postgresql://localhost/app_rehearsal')
```

The scratch database is usually empty, so the whole history replays rather than what is pending on the real one, which proves the migrations run from nothing. The dialect check does not apply, the changes may survive the rollback, and the footer says so. The connection closes when the command ends. On an engine whose schema changes do not roll back, the rehearsed objects stay behind, so recreate the scratch database before the next rehearsal.

The receipt belongs on the database `migrate` will read, not on the throwaway one, so the CLI writes it there after the scratch run passes. The key is computed against the real database's history and pending set. It is written only when the scratch run applied every migration pending on the real database; otherwise the output says the receipt was not recorded. A scratch rehearsal cannot cover a generated migration, because the diff it runs against the throwaway schema is not the diff the real run will produce.

Through the API, `rehearse(scratch=True)` writes nothing at all. Take the key off the result and record it yourself on a migrator bound to the real database:

```python
rehearsal = scratch_migrator.rehearse(scratch=True)
if rehearsal.ok:
    real_migrator.record_rehearsal(receipt_key(
        real_migrator.applied_records(), real_migrator.pending()
    ))
```

In Python, `migrator.rehearse()` returns a `Rehearsal`: a list of `RehearsalResult(id, up_ok, down_ok, error, landed, reversed)` with `key`, `recorded`, and `ok` on it. `down_ok` is `None` when nothing was proved, and `error` then says why. `landed` and `reversed` follow the same rule as the JSON output: `None` not checked, `[]` proved, a list of lines when it failed. Pass `rehearse(models=[User, Show])` to rehearse the model diff too, and `rehearse(scratch=True)` for a connection to a database you can throw away. `AsyncMigrator.rehearse()` is the same on an adapter, apart from `models`: diffing models against a database is a synchronous path, so the async rehearsal covers registered migrations only.

## Guards

A rehearsal proves a migration works. A guard decides whether you want it to run at all. Guards are your team's rules about SQL: no drops in a deploy, every index built concurrently, no run longer than fifty statements.

Give them to the migrator, or name them in the config module:

```python
from sustained.guards import index_must_be_concurrent, max_statements, no_drops

guards = [no_drops(), index_must_be_concurrent(), max_statements(50)]

migrator = Migrator(conn, migrations, dialect=Dialects.POSTGRES, guards=guards)
```

Each rule returns a verdict on each statement it objects to: `block` or `warn`. A `block` stops `up()` before a single statement runs, and raises `GuardBlocked`. A `warn` prints on stderr and the run goes on.

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

Both commands exit 3. There is no `--force`: a rule you can wave away on the day is not a rule. Fix the statement, or take the rule out of the list.

### The rules

| Rule | Verdict | Flags |
| --- | --- | --- |
| `no_drops()` | block | A statement that drops a table, column, view, schema, or database |
| `index_must_be_concurrent()` | block | `CREATE INDEX` without `CONCURRENTLY`, on Postgres only |
| `no_table_rewrite()` | warn | A column type change, or a NOT NULL with nothing to fill existing rows |
| `no_lock_without_timeout()` | block | A run that alters or drops a table with no `SET lock_timeout` in it |
| `max_statements(n)` | block | Every statement past the limit |

Every one is a factory, so they all read the same at the call site. `no_table_rewrite()` warns where the others block, because whether a change rewrites the table depends on the engine, its version, and whether the two types coerce. Read it against your own engine rather than trusting it.

`index_must_be_concurrent()` is silent on every dialect but Postgres, which is the only one with the keyword. A rule that does not apply says nothing rather than guessing.

### What guards read, and when

Guards read every SQL statement the run would apply: SQL file migrations, Python migrations with string steps, and the diff against your models. A callable step renders no SQL, so guards cannot see inside it, the same limit the destructive labels carry.

`migrate` checks twice. The registered migrations are checked before anything runs. The diff against the models cannot be generated until those have run, so its statements are checked the moment they exist, together with the ones already applied, so a rule about the whole run counts the whole run. A warning already printed is not printed again.

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

There is no rule language and no severity beyond the two verdicts. A guard is a Python function, so the rest is Python.

## Callbacks Around a Run

The migrator calls three functions around `up()`, whichever ones you give it:

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

`before_migrate` runs before the run starts, which is before validation and before the advisory lock. `after_migrate` runs only when at least one migration applied, so a run with nothing to do stays quiet. `on_error` runs after the failure and before it reaches the caller; if the callback itself raises, its error prints on stderr and the migration error is the one that propagates. `migration_id` names the migration that failed, or is `None` when the run failed before reaching one, which is what a guard block or a validation problem looks like.

Only `up()` calls them. `rehearse` does not, since nothing real happened. `AsyncMigrator` takes the same `Callbacks`, receives the adapter as the first argument, and awaits a callback that returns an awaitable, so `async def before_migrate(adapter)` works.

## Offline Review and Async

`migrator.script('up')` renders every statement a run would execute, including tracking bookkeeping, without touching the database, for review or DBA handoff; `script('down')` renders the rollback. For async services, `AsyncMigrator` in `sustained.aio_migrations` runs the same `Migration` objects on an `AsyncAdapter` with the same `up`, `down`, `down_to`, `status`, `statuses`, `validate`, `repair`, and `baseline` surface; callable steps receive the adapter and are awaited.
