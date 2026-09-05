---
layout: default
title: Migrations reference
---

`sustained.migrations`, `sustained.aio_migrations`, `sustained.autogenerate`, `sustained.migration_files`, `sustained.ddl`, and `sustained.analysis`. Import these names from their module path, because the package root does not re-export them; the `ddl` module itself imports as `from sustained import ddl`.

Guide: [Schema and Migrations](/schema).

## `Migration`

```python
Migration(id, up, down=..., checksum=None, repeatable=False, transactional=True)
```
{: .sig}

A `Migration` is one schema change. A step is a SQL string, a list of statements, a [ddl step](#typed-ddl-steps), or a callable that receives the connection. A list may mix strings and ddl steps.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | Fixes the order. Must not be empty. |
| `up` | step | What the migration does. |
| `down` | step or `None` | What reverses it. `None` means it cannot be reverted. |
| `checksum` | `str` or `None` | Pins a checksum. Needed only for a callable step, which has no SQL to hash. |
| `repeatable` | `bool` | Re-runs whenever its checksum changes, instead of running once. |
| `transactional` | `bool` | `False` runs the migration outside a transaction, for a statement the engine refuses inside one. |

When `down` is not given and `up` is a list of reversible ddl steps, the down step derives itself: the inverses of the up steps, newest first. An up step that includes an irreversible ddl step then raises `ValueError`, naming the step; pass an explicit down step, or `down=None` to declare the migration irreversible. An up step with no ddl steps in it derives nothing, and `down` stays `None` as before. Repeatables never derive a down step.

`transactional=False` covers the up step and the down step. The migrator turns the driver's own transaction control off for the migration, so a statement such as `CREATE INDEX CONCURRENTLY` on Postgres can run, and turns it back on after. The tracking row is written after the statements. Nothing rolls a failed one back: the statements that already ran stay applied, and the failure row makes validation stop the next `up()` until you clean up and run `repair()`. `AsyncMigrator` runs such a migration bare too, but an adapter over a driver with its own transaction control, such as `DbApiAsyncAdapter`, still opens a transaction; use `AsyncpgAdapter` there.

`Migration` raises `ValueError` when the id is empty, when a repeatable declares a `down` step, and when a repeatable has a callable step and no explicit `checksum`.

## Typed ddl steps

These names live in `sustained.ddl`. A `DdlStep` names one schema change and renders to SQL through a dialect compiler when the migration runs, so one migration serves every dialect. Build steps with the factories below, not with `DdlStep` directly.

| Signature | Reverses as |
| --- | --- |
| `create_table(model_or_name, columns=None, constraints=None, options=None, indexes=None)` | `drop_table`, dropping the columns' enum types after the table |
| `drop_table(model_or_name)` | irreversible |
| `add_column(table, name, column)` | `drop_column` |
| `drop_column(table, name)` | irreversible |
| `rename_column(table, old, new)` | the rename, backwards |
| `rename_table(old, new)` | the rename, backwards |
| `add_foreign_key(table, foreign_key)` | `drop_foreign_key` |
| `drop_foreign_key(table, name)` | irreversible |
| `add_check(table, check)` | `drop_constraint` |
| `drop_constraint(table, name)` | irreversible |
| `create_index(table, index)` | `drop_index` |
| `drop_index(table, name)` | irreversible |
| `create_enum(name, *values)` | `drop_enum` |
| `drop_enum(name)` | irreversible |
| `add_enum_value(name, value)` | irreversible: Postgres has no `DROP VALUE` |
| `set_column_comment(table, name, comment, previous=None, column=None)` | the same step with `previous`, when given |
| `sql(text)` | irreversible: one raw statement, rendered as written on every dialect |

A `table` argument takes a Model class or a table name string. `create_table(model)` reads the model's columns, constraints, options, and indexes when the step is built, so a later model edit changes the migration's checksum; pass explicit `columns` when the migration must outlive the model. The `column`, `check`, `foreign_key`, and `index` arguments take the same `ColumnDef`, `Check`, `ForeignKey`, and `Index` objects a model declares.

On a `DdlStep`, `render(compiler)` returns the SQL statements for that compiler's dialect, `reversible` says whether the step knows its inverse, `inverse()` returns that step or `None`, and `signature()` returns the canonical form the checksum hashes: the operation name and its arguments, serialized the same way on every dialect.

`create_enum`, `drop_enum`, and `add_enum_value` raise `DialectError` at render time on a dialect without named enum types. Each factory raises `ValueError` for a missing name or an empty argument.

`set_column_comment` sets one column's comment, and `comment=None` clears it. Pass `previous` as the column's current comment, `None` when it has none, and the step reverses by setting it back. MySQL changes a comment by restating the whole column with `MODIFY COLUMN`, so there the step also needs the column's `ColumnDef` as `column`. The restatement includes the type, nullability, default, and identity, and never restates UNIQUE, which would add a second index on each run. Dialects that store no column comments raise `DialectError` at render time, and Athena raises because it cannot change a comment in place.

Guide: [Typed migration steps](/schema#typed-migration-steps).

## `Migrator`

```python
Migrator(connection, migrations, table='sustained_migrations', dialect=Dialects.DEFAULT, tracking_table_options=None, rehearsal_table='sustained_rehearsals', guards=None, callbacks=None)
```
{: .sig}

`Migrator` applies and reverts migrations, records them in a tracking table, and runs each migration inside a transaction. Duplicate ids raise `ValueError`.

`guards` is a list of rules over the statements an up run would apply. See [Guards](#guards) below. `callbacks` is a `Callbacks` object, whose functions `up()` calls around the run.

`Migrator` exposes `connection`, `dialect`, and `compiler` as properties. The migrator renders ddl steps through that compiler.

### Inspecting

```python
applied_records() -> list[AppliedRecord]
```
{: .sig #applied_records}

Every tracking row, ordered by sequence. Creates or upgrades the tracking table first.

```python
applied() -> list[str]
```
{: .sig #applied}

The ids that ran successfully, in order.

```python
pending() -> list[Migration]
```
{: .sig #pending}

Versioned migrations with no successful row, then repeatables that are new or changed.

```python
status() -> list[tuple[str, bool]]
```
{: .sig #status}

`(id, applied)` per registered migration.

```python
statuses() -> list[tuple[str, str]]
```
{: .sig #statuses}

`(id, state)`, where state is `applied`, `pending`, or `changed`. `changed` marks a repeatable whose contents differ from its last run.

### Running

```python
up(target=None, validate=True, allow_out_of_order=False, models=None, unrehearsed=False, ...) -> list[str]
```
{: .sig #up}

Validates, then applies pending migrations in order. `target` stops after that id and skips the repeatables. With `models`, the diff against them runs after the versioned migrations and before the repeatables; it cannot be combined with `target`. `unrehearsed=True` waives the rehearsal gate below. The remaining options are the [diff options](#generating-from-models) below.

```python
down(steps=1) -> list[str]
```
{: .sig #down}

Reverts newest-first. Never touches repeatables.

```python
down_to(target) -> list[str]
```
{: .sig #down_to}

Reverts until `target` is the newest applied.

```python
baseline(target) -> list[str]
```
{: .sig #baseline}

Records migrations up to and including `target` as applied, without running them. Also records every repeatable at its current checksum.

`up` raises `MigrationError` when validation finds problems, `RehearsalRequired` when the run would remove data and no passing rehearsal row covers it, and `ValueError` for an unknown target or a target that names a repeatable. `down` and `down_to` raise `ValueError` when an applied migration is not registered, and when it has no down step.

A migration that fails re-raises the driver's exception with a `migration_id` attribute attached, so the caller can tell which migration failed.

### Validating and repairing

```python
validate(raise_on_problems=True) -> list[str]
```
{: .sig #validate}

Checks the tracking table against the registry. Raises `MigrationError` unless `raise_on_problems=False`.

```python
repair() -> list[str]
```
{: .sig #repair}

Deletes failed-attempt rows and rewrites drifted or missing checksums. Returns the actions taken.

The problems validation reports:

- A migration has a failed attempt on record.
- An applied id is not registered with this migrator.
- A checksum no longer matches, which means the migration changed after it ran.
- A pending migration is ordered before an applied one. `allow_out_of_order=True` accepts that order.

`repair()` fixes the bookkeeping only. It does not undo the schema changes a failed attempt left behind, and it leaves repeatable checksums alone, because a changed checksum schedules the re-run.

### Generating from models

```python
plan(models, ...) -> Migration | None
```
{: .sig #plan}

The migration `up(models=[...])` would generate. Records nothing, applies nothing. `None` when the schema is current.

```python
up(models=[...], ...) -> list[str]
```
{: .sig #up-models}

Generates, registers, and applies it, with everything else pending.

```python
drift(models, renames=None, table_renames=None) -> list[str]
```
{: .sig #drift}

What the models still ask for, one readable line each. Empty when the database contains everything they declare.

```python
sync(models, ...) -> list[str]
```
{: .sig #sync}

Deprecated since 2.13.0, removed in 3.0. Warns, then calls `up(models=...)`.

These methods take the same options:

| Option | Default | Meaning |
| --- | --- | --- |
| `allow_drops` | `False` | Generate drops for tables and columns the models do not declare. Without it, they are left alone. |
| `ignore_changed_columns` | `False` | Skip type and nullability differences entirely. |
| `migration_id` | generated | The id. Defaults to `auto_<UTC timestamp>`. |
| `renames` | `None` | `{'table.old': 'new'}`, so a rename is a rename and not a drop plus an add. |
| `table_renames` | `None` | `{'old': 'new'}`. |
| `type_casts` | `None` | `{'table.col': 'col::integer'}`, a `USING` hint. Postgres only. |
| `ignore_undeclared` | `True` | Leave objects the models do not declare alone. `False` refuses to generate while any exist. |

Pass every model you manage. These methods compare the whole database against the whole list, so a table missing from the list is a table nothing keeps up to date. The tracking table is always excluded from the comparison.

### Rehearsing

```python
rehearse(scratch=False, models=None, ...) -> Rehearsal
```
{: .sig #rehearse}

`rehearse()` applies every pending migration, runs the down steps back down, and rolls the whole run back. It returns an empty `Rehearsal` when nothing is pending. With `models`, the migration generated from those models joins the run without being registered, and the remaining arguments are the diff options above.

`rehearse()` reads the schema before the run and again after the down sweep, so it reports a down step that runs without taking its change back. The comparison covers tables and columns. It does not cover indexes, constraints, or column defaults.

`rehearse()` raises `ValueError` when:

- The dialect's schema changes do not roll back. Only the default dialect, Postgres, and DuckDB qualify. Pass `scratch=True` to waive the check for a connection to a database you can throw away.
- The connection is in autocommit mode.
- The call sits inside an open `transaction()` block, because the rollback would take the caller's work back as well.

The check reads the declared dialect rather than the engine. A config that leaves the dialect unset while it points at MySQL would rehearse for real.

### Rehearsal rows

```python
record_rehearsal(key, outcome='passed')
```
{: .sig #record_rehearsal}

Writes the rehearsal row for one key, replacing any earlier row. `outcome` is `'passed'`, `'failed'`, or `'override'` for statements applied with `unrehearsed=True`; anything else raises `ValueError`.

```python
rehearsal_outcome(key) -> str | None
```
{: .sig #rehearsal_outcome}

What the recorded rehearsal proved, or `None` when none covers the key.

```python
rehearsed(key) -> bool
```
{: .sig #rehearsed}

Whether a passing rehearsal covers the key.

A passing `rehearse()` records its own row and returns the key on the result. It also records a row for each shorter run a `target` would produce that removes data, because the rehearsal applied and reverted those statements on its way through. `rehearse(scratch=True)` records nothing, because the row belongs on the database the next run reads. Record that row there yourself.

`up()` reads a rehearsal row before it applies any statement that removes data, and raises `RehearsalRequired` when no row covers the content. A callable step renders no SQL, so a callable step never triggers the check.

```python
rehearsal_key(applied, run) -> str
```
{: .sig #rehearsal_key}

`rehearsal_key()` computes the key both sides use: a SHA-256 over the checksums of the successful rows in `applied`, then over the checksums of the migrations in `run`. It hashes an id only for a callable step with no checksum, as the token `id:<id>`.

This function was called `receipt_key()` before version 2.20.0, and the outcome constants were `RECEIPT_PASSED`, `RECEIPT_FAILED`, and `RECEIPT_OVERRIDE`. The old names still import from `sustained.migrations` and raise a `DeprecationWarning`. Version 3.0 removes them.

### Rendering without running

```python
script(direction='up') -> str
```
{: .sig #script}

`script()` returns every statement a run would execute as text, including the tracking bookkeeping. Any direction other than `up` or `down` raises `ValueError`.

## Result types

`AppliedRecord(id, seq, checksum, success, generated)` is one tracking row. `generated` marks a row that a model diff wrote.

`RehearsalResult(id, up_ok, down_ok, error, landed, reversed)` records what a rehearsal proved about one migration. `up_ok` is `None` for a migration the rehearsal left out: one with `transactional=False` runs outside a transaction, and the rehearsal cannot roll such a run back. `down_ok` is `None` when the rehearsal proved nothing, and `error` then says why: `no down step`, `no down step (repeatable)`, `down not reached: ...`, or `down not rehearsed: the run stopped`.

`landed` and `reversed` are `None` when the check did not run, `[]` when the check passed, and a list of readable lines when the check failed. `landed` is filled for the generated migration only. `reversed` is filled for every migration whose down step ran.

`rehearse()` returns a `Rehearsal`, which subclasses `list` over those results, so it iterates and indexes like a list. It adds the attributes below.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `key` | `str` | The rehearsal key for the set the rehearsal ran. |
| `recorded` | `bool` | Whether the row was written. `False` after `scratch=True`. |
| `ok` | `bool` | Whether every result passed. |

`ok` uses the module function `rehearsal_failed(result)`. A result fails when its up step raised, when its down step failed, when the models did not land, or when the schema did not come back. A down step the rehearsal could not prove is not a failure.

## The tracking table

The tracking table is named `sustained_migrations` by default and has these columns:

| Column | Type | Contains |
| --- | --- | --- |
| `id` | `VARCHAR(255)` primary key | The migration id |
| `seq` | `INTEGER` | The apply order |
| `checksum` | `VARCHAR(64)` | SHA-256 of the up statements |
| `applied_at` | `TEXT` not null | When it ran |
| `execution_ms` | `INTEGER` | How long it took. Null for a baselined row |
| `success` | `BOOLEAN` not null | Whether it finished |
| `generated` | `BOOLEAN` | Whether a model diff wrote it. Such a row is never reported as an unregistered migration |
| `steps` | `TEXT` | The up and down statements of a generated migration, as JSON. Null for every registered one, whose statements live in your code or your migrations directory |

On Athena the same columns are all plain and nullable, because Athena enforces no constraints. A tracking table written by an earlier version, with only `id` and `applied_at`, upgrades in place on first use. A generated row written before the `steps` column existed contains no statements, so `down()` cannot revert that row.

## The rehearsal table

The rehearsal table is named `sustained_rehearsals` by default, is created on first use, and has these columns:

| Column | Type | Contains |
| --- | --- | --- |
| `rehearsal_key` | `VARCHAR(64)` primary key | The key `rehearsal_key()` computes |
| `outcome` | `VARCHAR(16)` not null | `passed` or `failed` |
| `rehearsed_at` | `TEXT` not null | When the rehearsal ran |

Every diff against the models excludes both tables, so neither table reads as drift, and neither reads as an object a down step left behind.

## Module functions

```python
migration_checksum(migration) -> str | None
```
{: .sig #migration_checksum}

The checksum validation compares. A ddl step hashes as its canonical signature, so the checksum is the same on every dialect. `None` for a callable step with no explicit checksum.

```python
create_table_migration(model) -> Migration
```
{: .sig #create_table_migration}

A create/drop pair derived from a model.

```python
migration_sql(migration, direction='up', compiler=None) -> list[str]
```
{: .sig #migration_sql}

One migration's statements, for offline review. Ddl steps render for the given compiler's dialect, or ANSI when none is given. A callable step renders as a comment. Raises `ValueError` when that step is `None`.

```python
rehearsal_failed(result) -> bool
```
{: .sig #rehearsal_failed}

Whether one result stops a rehearsal from passing.

```python
run_statements(run, compiler=None) -> list[MigrationStatement]
```
{: .sig #run_statements}

Every up statement a run would apply, callable steps skipped. Ddl steps render for the given compiler's dialect, which is how the guards read them. Each statement is a [`MigrationStatement`](#migration-statement) naming the migration it came from.

```python
check_guards(guards, run, dialect, reported=None)
```
{: .sig #check_guards}

Runs the guards over a run. Raises `GuardBlocked` on a blocking verdict, prints warnings on stderr.

### `Callbacks`

```python
Callbacks(before_migrate=None, after_migrate=None, on_error=None)
```
{: .sig}

`Callbacks` is a NamedTuple of optional functions that you pass to either migrator. `before_migrate(connection)` runs before validation and before the advisory lock. `after_migrate(connection, applied)` runs only when at least one migration applied. `on_error(connection, migration_id, error)` runs after a failure, and its `migration_id` argument is `None` when the run failed before it reached a migration. When `on_error` itself raises, its error prints on stderr, and the run's error still propagates. A `before_migrate` or `after_migrate` that raises stops the caller.

## Guards

Guards live in `sustained.guards`. A guard is a `Callable[[Sequence[MigrationStatement], Dialects], list[Verdict]]`. It reads the statements an up run would apply and returns one `Verdict(rule, verdict, statement)` per objection, where the `verdict` field is `BLOCK` (`'block'`) or `WARN` (`'warn'`). A `MigrationStatement` (in `sustained.analysis`) is a `str` that also records `migration_id`, the migration the statement came from, and `transactional`, that migration's transaction flag. A guard typed against `Sequence[str]` still fits the type and still runs, because the statements are strings. `statement_scope(statement)` returns the pair for one statement, and gives `(None, True)` for a plain string.

`up()` raises `GuardBlocked` on a blocking verdict, before any statement runs, and prints warnings on stderr. A callable step renders no SQL, so guards cannot read it. `down()` runs no guards, because a down step undoes work the rules already passed, and `no_drops()` would block every rollback of a create.

```python
no_drops() -> Guard
```
{: .sig #no_drops}

Blocks a table, column, view, materialized view, schema, database, enum type, or constraint drop. Index and key drops pass.

```python
index_must_be_concurrent() -> Guard
```
{: .sig #index_must_be_concurrent}

Blocks `CREATE INDEX` without `CONCURRENTLY`. Postgres only; silent elsewhere.

Postgres refuses `CREATE INDEX CONCURRENTLY` inside a transaction block, so the index needs a migration with `transactional=False`, or a SQL file with the `-- sustained: no transaction` marker.

```python
no_table_rewrite() -> Guard
```
{: .sig #no_table_rewrite}

Warns on a column type change, or a NOT NULL with no default for existing rows.

```python
no_lock_without_timeout() -> Guard
```
{: .sig #no_lock_without_timeout}

Blocks a statement that alters or drops a table with no `SET lock_timeout` in force before it. A timeout later in the run does not cover it. Postgres only; silent elsewhere.

A plain `SET lock_timeout`, with or without SESSION, covers the rest of the run. A `SET LOCAL lock_timeout` covers only the statements after it in its own migration, because the commit that ends the migration drops the setting. In a migration with `transactional=False` there is no transaction block in which a LOCAL setting could apply, so the rule counts it for nothing. Use the plain form there.

```python
max_statements(limit) -> Guard
```
{: .sig #max_statements}

Blocks every statement past `limit`. A limit below 1 raises `ValueError`.

```python
run_guards(guards, statements, dialect) -> list[Verdict]
```
{: .sig #run_guards}

Every guard's verdicts, in guard order. A plain string is wrapped in a `MigrationStatement` that names no migration first, so every guard reads the same kind of value.

```python
blocking(verdicts) -> list[Verdict]
```
{: .sig #blocking}

The verdicts that stop a run.

```python
warnings_only(verdicts) -> list[Verdict]
```
{: .sig #warnings_only}

The verdicts that only report.

The scan is textual, the same way the destructive labels are. Sustained strips comments, empties quoted text, collapses whitespace, and parses no SQL. The verdict prints the statement with its quoted text intact.

## `AsyncMigrator`

`AsyncMigrator` lives in `sustained.aio_migrations`.

```python
AsyncMigrator(adapter, migrations, table='sustained_migrations', dialect=Dialects.DEFAULT, tracking_table_options=None, rehearsal_table='sustained_rehearsals', guards=None, callbacks=None)
```
{: .sig}

`AsyncMigrator` is the same runner on an `AsyncAdapter`: the same tracking table, the same `Migration` objects, and the same validation rules and refusal messages. Guards and callbacks work the same way, except that a callback receives the adapter, and is awaited when it returns an awaitable. `AsyncMigrator` exposes `adapter`, `dialect`, and `compiler` as properties.

Every method is a coroutine:

- `applied_records`
- `read_applied_records`
- `applied`
- `read_applied`
- `pending`
- `status`
- `statuses`
- `validate`
- `repair`
- `baseline`
- `up`
- `rehearse`
- `down`
- `down_to`
- `record_rehearsal`
- `rehearsal_outcome`
- `rehearsed`
- `script`
- `plan`
- `drift`

Both migrators compute the key the same way, so a row written by one migrator opens the gate for the other on the same database.

`await migrator.script('up')` renders the same text `Migrator.script('up')` renders, and writes nothing, not even the tracking table. `read_applied_records()` and `read_applied()` read the rows the same way.

`await migrator.plan(models)` and `await migrator.drift(models)` diff the models against the database and return what `Migrator.plan()` and `Migrator.drift()` return. Both read the schema through the adapter and write nothing. The schema read is the only statement `plan()` runs, so it cannot ask whether a table contains rows. A table it cannot read counts as one that contains rows, so a new NOT NULL column with no `default` and no `backfill` is refused here even on an empty table, where `Migrator.plan()` adds it.

`up(models=[...])` and `rehearse(models=[...])` take the same arguments the synchronous ones take, and behave the same way: the generated migration runs last of the versioned ones, its statements go on its tracking row rather than into the migrations directory, and it joins the registered list only after it applied.

A callable step receives the adapter rather than a connection, and its return value is awaited when it is awaitable.

## Migrations as SQL files

These names live in `sustained.migration_files`.

```python
load_migrations(directory, placeholders=None) -> list[Migration]
```
{: .sig #load_migrations}

`load_migrations` reads the `<id>.up.sql` files first, each one optionally paired with `<id>.down.sql`, sorted by id. Then it reads the `<id>.repeat.sql` repeatables, also sorted by id. Statements split at line-ending semicolons, with or without a `--` comment after the semicolon, so a semicolon inside a string literal is left intact. A body with its own statements, such as a trigger or a procedure, is split apart.

A `-- sustained: no transaction` line of its own in an up file or a repeat file sets `transactional=False` on that migration. Case does not matter, and the two words may be joined by a space, a hyphen, or an underscore. `declares_no_transaction(text)` reports the same thing for one file's text. The marker is read from the up file and the repeat file only; the flag already covers the down step.

`load_migrations` raises `ValueError` for a missing directory, for a file that matches none of the naming patterns, for an id with both an up file and a repeat file, for a down file with no up file, and for an empty up, down, or repeat file. The naming check reads every file in the directory, whatever its extension, so a misnamed migration such as `0002_add.up.sq` raises instead of loading nothing. It passes over subdirectories, dotfiles, and editor backup files (`*~`, `*.bak`, `*.orig`, `*.swp`, `*.swo`, `*.tmp`); every other file must follow a naming pattern, so keep a README outside the migrations directory.

```python
substitute_placeholders(text, placeholders, source) -> str
```
{: .sig #substitute_placeholders}

`substitute_placeholders` fills the `${key}` markers. Write `$${` for a literal `${`. The function returns the text unchanged when `placeholders` is `None`. It raises `ValueError`, naming the file, for an unknown key or a malformed marker.

Passing a mapping turns substitution on, including an empty mapping. Substitution happens before Sustained computes the checksum, so the checksum covers the SQL that ran.

```python
split_sql_statements(text) -> list[str]
```
{: .sig #split_sql_statements}

`split_sql_statements` splits on line-ending semicolons, including a semicolon with a `--` comment after it, and drops the pieces that are only whitespace or comments.

## Autogeneration internals

These names live in `sustained.autogenerate`. `plan()` and `up(models=[...])` are built on top of them.

```python
diff_schema(connection, models, dialect=Dialects.DEFAULT, exclude_tables=('sustained_migrations',), renames=None, table_renames=None, snapshot=None) -> SchemaDiff
```
{: .sig #diff_schema}

Changes nothing and reports every difference, drops included. Pass `snapshot` to compare against a schema you already read with `introspect_schema()`; the connection is then not touched. The rename hints are applied to that snapshot in place, so you see the same renamed schema the diff compares against.

```python
autogenerate(connection, models, id, dialect=..., allow_drops=False, ignore_changed_columns=False, exclude_tables=..., renames=None, table_renames=None, type_casts=None, ignore_undeclared=False) -> Migration | None
```
{: .sig #autogenerate}

Builds the migration a diff asks for. Refuses to generate the lossy differences, and refuses to run at all while the database contains objects the models do not declare, unless you pass `allow_drops=True` or `ignore_undeclared=True`. The migrator passes `ignore_undeclared=True`. A CHECK constraint no model declares is the one object that never refuses: it comes back as a note on the diff, because engines rewrite check expressions and the comparison cannot justify a refusal.

```python
introspect_schema(connection, dialect=Dialects.DEFAULT, schemas=()) -> dict[str, IntrospectedTable]
```
{: .sig #introspect_schema}

Reads the live schema. `await async_introspect_schema(adapter, dialect=Dialects.DEFAULT, schemas=())` is the same read on an adapter. The read covers the schema the connection is on, plus every name in `schemas`; `diff_schema()` fills that from the models' `tableSchema`. See [Schema scope](/dialects#schema-scope).

```python
diff_snapshots(before, after) -> list[str]
```
{: .sig #diff_snapshots}

One line per difference between two introspected schemas. Tables and columns only.

```python
normalize_type(raw) -> str
```
{: .sig #normalize_type}

The canonical spelling of a reported column type, for comparison.

```python
normalize_default(raw) -> str | None
```
{: .sig #normalize_default}

The canonical spelling of a reported column default, for comparison. Balanced outer parentheses, a Postgres `::type` cast with its length, quotes, and an empty argument list all come off. A `nextval(...)` default returns `None`, because Postgres reports it for a serial column and no model declaration can equal it, so the diff reports nothing for it.

### `SchemaDiff`

| Attribute | Contains |
| --- | --- |
| `missing_tables` | Models with no table |
| `new_columns` | `(model, name, ColumnDef)` |
| `extra_tables` | Table names the models do not declare |
| `extra_columns` | `(table, column)` |
| `changed_columns` | `(table, column, actual, expected)` |
| `new_indexes`, `extra_indexes`, `changed_indexes` | Index differences |
| `new_enum_types` | `(name, values)` for enum types the models declare and the database lacks |
| `changed_enum_types` | `(name, live_values, declared_values)` for enum types whose values differ |
| `new_foreign_keys`, `changed_foreign_keys`, `extra_foreign_keys` | Foreign key differences, by constraint name |
| `new_checks`, `changed_checks`, `extra_checks` | CHECK constraint differences, by constraint name |
| `constraint_notes` | Differences that are reported but never auto-migrated |

`is_empty()` returns whether the diff found any difference. `summary()` returns one readable line per difference, with the destructive ones marked, or `schema up to date` when there is no difference.

The enum buckets fill on the dialects with named types. Postgres compares against `pg_enum`, and DuckDB against `duckdb_types()`. A DuckDB too old for that view falls back to reading the values from the column's inline type spelling, and a type with no column left reads as absent there. Missing foreign keys and checks generate `ADD CONSTRAINT`; changed and extra ones are gated by `allow_drops`. Primary key set changes, column-level UNIQUE, and default differences always land in `constraint_notes`, and a Postgres check expression whose difference remains after normalization goes there too. Generation never migrates a note for you.

### What generation refuses

`autogenerate()` raises `ValueError` instead of guessing, for each of these:

- A drop without `allow_drops=True`. The message names the objects and the flag.
- Tightening a column to NOT NULL with no `backfill` or `default`.
- Adding a NOT NULL column with no `backfill` or `default`.
- Adding a primary key or autoincrement column, which ALTER TABLE cannot do.

A migration that includes a drop has no down step, and neither does one that includes a SQLite table rebuild, because neither one reverses.

## Analysis

These names live in `sustained.analysis`, and `sustained plan` uses them.

```python
destructive_statements(statements) -> list[str]
```
{: .sig #destructive_statements}

The statements that remove data or an object that contains it: `DROP TABLE`, `DROP COLUMN`, `DROP TYPE`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, `DROP SCHEMA ... CASCADE`, a constraint drop, `TRUNCATE`, and `DELETE FROM`. Comments removed, whitespace collapsed. Skips index and key drops, and a plain `DROP SCHEMA`, which refuses a non-empty schema.

```python
summarize(migration, state, compiler=None) -> PendingSummary
```
{: .sig #summarize}

One migration reduced to its id, state, repeatable flag, statement count, and destructive statements. Ddl steps render for the given compiler's dialect, or ANSI when none is given.

`PendingSummary(id, state, repeatable, sql, destructive)` contains that summary. `sql` is `None` for a callable step, which has no SQL to count. Each statement in it is a `MigrationStatement`.

```python
MigrationStatement(statement, migration_id=None, transactional=True)
```
{: .sig #migration-statement}

One statement with the migration it came from. It subclasses `str`, so anything that reads statements as strings reads these too. `migration_id` is the migration's id, or `None` when nothing named one. `transactional` is that migration's transaction flag.

```python
statement_scope(statement) -> tuple[str | None, bool]
```
{: .sig #statement_scope}

The migration id and transaction flag of one statement. A plain `str` gives `(None, True)`.

The scan is textual. It labels a column drop written without the COLUMN keyword, which MySQL allows. It keeps comments and quoted text out of the scan, so a drop named inside a string literal is not labelled. The label is a report for the operator, and `migrate` reads the same list for its rehearsal gate.
