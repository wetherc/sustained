---
layout: default
title: Migrations reference
---

`sustained.migrations`, `sustained.aio_migrations`, `sustained.autogenerate`,
`sustained.migration_files`, and `sustained.analysis`. None of these are
re-exported at the package root; import from the module path.

Guide: [Schema and Migrations](/schema).

## `Migration`

```python
Migration(id, up, down=None, checksum=None, repeatable=False)
```

One schema change. A step is a SQL string, a list of statements, or a callable
that receives the connection.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | Fixes the order. Must not be empty. |
| `up` | step | What the migration does. |
| `down` | step or `None` | What reverses it. `None` means it cannot be reverted. |
| `checksum` | `str` or `None` | Pins a checksum. Needed only for a callable step, which has no SQL to hash. |
| `repeatable` | `bool` | Re-runs whenever its checksum changes, instead of running once. |

Raises `ValueError` when the id is empty, when a repeatable declares a `down`
step, or when a repeatable has a callable step and no explicit `checksum`.

## `Migrator`

```python
Migrator(connection, migrations, table='sustained_migrations',
         dialect=Dialects.DEFAULT, tracking_table_options=None,
         rehearsal_table='sustained_rehearsals', guards=None,
         callbacks=None)
```

Applies and reverts migrations, records them in a tracking table, and runs
each inside a transaction. Duplicate ids raise `ValueError`.

`guards` is a list of rules over the statements an up run would apply; see
[Guards](#guards) below. `callbacks` is a `Callbacks` object, whose functions
`up()` calls around the run.

Properties: `connection`, `dialect`.

### Inspecting

| Signature | Returns | Description |
| --- | --- | --- |
| `applied_records()` | `list[AppliedRecord]` | Every tracking row, ordered by sequence. Creates or upgrades the tracking table first. |
| `applied()` | `list[str]` | The ids that ran successfully, in order. |
| `pending()` | `list[Migration]` | Versioned migrations with no successful row, then repeatables that are new or changed. |
| `status()` | `list[tuple[str, bool]]` | `(id, applied)` per registered migration. |
| `statuses()` | `list[tuple[str, str]]` | `(id, state)`, where state is `applied`, `pending`, or `changed`. `changed` marks a repeatable whose contents differ from its last run. |

### Running

| Signature | Returns | Description |
| --- | --- | --- |
| `up(target=None, validate=True, allow_out_of_order=False, models=None, unrehearsed=False, ...)` | `list[str]` | Validates, then applies pending migrations in order. `target` stops after that id and skips the repeatables. With `models`, the diff against them runs after the versioned migrations and before the repeatables; it cannot be combined with `target`. `unrehearsed=True` waives the rehearsal gate below. The remaining options are the diff options below. |
| `down(steps=1)` | `list[str]` | Reverts newest-first. Never touches repeatables. |
| `down_to(target)` | `list[str]` | Reverts until `target` is the newest applied. |
| `baseline(target)` | `list[str]` | Records migrations up to and including `target` as applied, without running them. Also records every repeatable at its current checksum. |

`up` raises `MigrationError` when validation finds problems, `RehearsalRequired`
when the run would remove data and no passing rehearsal row covers it, and
`ValueError` for an unknown target or a target naming a repeatable. `down` and `down_to`
raise `ValueError` when an applied migration is not registered, or has no down
step.

A migration that fails re-raises the driver's exception with a `migration_id`
attribute attached, so the caller can tell which one it was.

### Validating and repairing

| Signature | Returns | Description |
| --- | --- | --- |
| `validate(raise_on_problems=True)` | `list[str]` | Checks the tracking table against the registry. Raises `MigrationError` unless `raise_on_problems=False`. |
| `repair()` | `list[str]` | Deletes failed-attempt rows and rewrites drifted or missing checksums. Returns the actions taken. |

The four problems validation reports:

- A migration has a failed attempt on record.
- An applied id is not registered with this migrator.
- A checksum no longer matches: the migration changed after it ran.
- A pending migration is ordered before an applied one. `allow_out_of_order=True`
  accepts it.

`repair()` fixes bookkeeping only. It does not undo schema changes a failed
attempt left behind, and it leaves repeatable checksums alone, because a
changed checksum is what schedules the re-run.

### Generating from models

| Signature | Returns | Description |
| --- | --- | --- |
| `plan(models, ...)` | `Migration` or `None` | The migration `up(models=[...])` would generate. Records nothing, applies nothing. `None` when the schema is current. |
| `up(models=[...], ...)` | `list[str]` | Generates, registers, and applies it, with everything else pending. |
| `drift(models, renames=None, table_renames=None)` | `list[str]` | What the models still ask for, one readable line each. Empty when the database holds everything they declare. |
| `sync(models, ...)` | `list[str]` | Deprecated since 2.13.0, removed in 3.0. Warns, then calls `up(models=...)`. |

Both take the same options:

| Option | Default | Meaning |
| --- | --- | --- |
| `allow_drops` | `False` | Generate drops for tables and columns the models do not declare. Without it, they are left alone. |
| `ignore_changed_columns` | `False` | Skip type and nullability differences entirely. |
| `migration_id` | generated | The id. Defaults to `auto_<UTC timestamp>`. |
| `renames` | `None` | `{'table.old': 'new'}`, so a rename is a rename and not a drop plus an add. |
| `table_renames` | `None` | `{'old': 'new'}`. |
| `type_casts` | `None` | `{'table.col': 'col::integer'}`, a `USING` hint. Postgres only. |
| `ignore_undeclared` | `True` | Leave objects the models do not declare alone. `False` refuses to generate while any exist. |

Pass every model you manage. Both compare the whole database against the whole
list, and a table missing from the list is one nothing keeps up to date. The
tracking table is always excluded.

### Rehearsing

```python
rehearse(scratch=False, models=None, ...) -> Rehearsal
```

Applies every pending migration, runs the down steps back down, and rolls the
whole thing back. Returns an empty `Rehearsal` when nothing is pending. With
`models`, the migration generated from them joins the run without being
registered, and the remaining arguments are the diff options above.

The schema is read before the run and again after the down sweep, so a down
step that runs without taking its change back is reported. Tables and columns
are compared; indexes, constraints, and column defaults are not.

Refuses, with `ValueError`, when:

- The dialect is not one whose schema changes roll back. Only the default
  dialect, Postgres, and DuckDB qualify. `scratch=True` waives this for a
  connection to a database you can throw away.
- The connection is in autocommit mode.
- The call is inside an open `transaction()` block, because its rollback would
  take the caller's work back too.

The check reads the declared dialect, not the engine. A config that leaves the
dialect unset while pointing at, say, MySQL would rehearse for real.

### Rehearsal rows

| Signature | Returns | Description |
| --- | --- | --- |
| `record_rehearsal(key, outcome='passed')` | `None` | Writes the rehearsal row for one key, replacing any earlier row. `outcome` is `'passed'`, `'failed'`, or `'override'` for statements applied with `unrehearsed=True`; anything else raises `ValueError`. |
| `rehearsal_outcome(key)` | `str` or `None` | What the recorded rehearsal proved, or `None` when none covers the key. |
| `rehearsed(key)` | `bool` | Whether a passing rehearsal covers the key. |

A passing `rehearse()` records its own row and returns the key on the
result. It also records one for each shorter run a `target` would produce that
removes data, since the rehearsal applied and reverted those on its way
through. `rehearse(scratch=True)` records nothing, because the row belongs
on the database the next run will read; record it there yourself.

`up()` reads a rehearsal row before it applies any statement that removes data, and
raises `RehearsalRequired` when none covers the content. A callable step
renders no SQL, so it never triggers the check.

```python
rehearsal_key(applied, run) -> str
```

The key both sides compute: a SHA-256 over the checksums of the successful
rows in `applied`, then the checksums of the migrations in `run`. Ids are
hashed only for a callable step with no checksum, as the token `id:<id>`.

This function was called `receipt_key()` before version 2.20.0, and the
outcome constants were `RECEIPT_PASSED`, `RECEIPT_FAILED`, and
`RECEIPT_OVERRIDE`. The old names still import from `sustained.migrations`,
raise a `DeprecationWarning`, and are removed in 3.0.

### Rendering without running

```python
script(direction='up') -> str
```

Every statement a run would execute, tracking bookkeeping included, as text.
Any direction other than `up` or `down` raises `ValueError`.

## Result types

`AppliedRecord(id, seq, checksum, success, generated)` holds one tracking row.
`generated` marks a row a model diff wrote.

`RehearsalResult(id, up_ok, down_ok, error, landed, reversed)` holds what a
rehearsal proved about one migration. `down_ok` is `None` when nothing was
proved, and `error` then says why: `no down step`, `no down step (repeatable)`,
`down not reached: ...`, or `down not rehearsed: the run stopped`.

`landed` and `reversed` are `None` when the check did not run, `[]` when it
passed, and a list of readable lines when it failed. `landed` is filled for the
generated migration only; `reversed` for every migration whose down step ran.

`Rehearsal` is what `rehearse()` returns: a `list` of those results, so it
iterates and indexes like one, with three additions.

| Attribute | Type | Meaning |
| --- | --- | --- |
| `key` | `str` | The rehearsal key for the set the rehearsal ran. |
| `recorded` | `bool` | Whether the row was written. `False` after `scratch=True`. |
| `ok` | `bool` | Whether every result passed. |

`rehearsal_failed(result)` is the module function behind `ok`: a result fails
when its up step raised, its down step failed, the models did not land, or the
schema did not come back. A down step that could not be proved is not a
failure.

## The tracking table

Eight columns, named by default `sustained_migrations`:

| Column | Type | Holds |
| --- | --- | --- |
| `id` | `VARCHAR(255)` primary key | The migration id |
| `seq` | `INTEGER` | The apply order |
| `checksum` | `VARCHAR(64)` | SHA-256 of the up statements |
| `applied_at` | `TEXT` not null | When it ran |
| `execution_ms` | `INTEGER` | How long it took. Null for a baselined row |
| `success` | `BOOLEAN` not null | Whether it finished |
| `generated` | `BOOLEAN` | Whether a model diff wrote it. Such a row is never reported as an unregistered migration |
| `steps` | `TEXT` | The up and down statements of a generated migration, as JSON. Null for every registered one, whose statements live in your code or your migrations directory |

On Athena the same columns are all plain and nullable, because Athena
enforces no constraints. Tracking tables written by earlier versions, holding
only `id` and `applied_at`, upgrade in place on first use. A generated row
written before the `steps` column existed carries no statements, so `down()`
cannot revert it.

## The rehearsal table

Three columns, named by default `sustained_rehearsals`, created on first use:

| Column | Type | Holds |
| --- | --- | --- |
| `rehearsal_key` | `VARCHAR(64)` primary key | The key `rehearsal_key()` computes |
| `outcome` | `VARCHAR(16)` not null | `passed` or `failed` |
| `rehearsed_at` | `TEXT` not null | When the rehearsal ran |

Both tables are excluded from every diff against the models, so neither reads
as drift or as an object a down step left behind.

## Module functions

| Signature | Returns | Description |
| --- | --- | --- |
| `migration_checksum(migration)` | `str` or `None` | The checksum validation compares. `None` for a callable step with no explicit checksum. |
| `create_table_migration(model)` | `Migration` | A create/drop pair derived from a model. |
| `migration_sql(migration, direction='up')` | `list[str]` | One migration's statements, for offline review. A callable step renders as a comment. Raises `ValueError` when that step is `None`. |
| `rehearsal_key(applied, run)` | `str` | The key a rehearsal row is stored under. |
| `rehearsal_failed(result)` | `bool` | Whether one result stops a rehearsal from passing. |
| `run_statements(run)` | `list[str]` | Every up statement a run would apply, callable steps skipped. |
| `check_guards(guards, run, dialect, reported=None)` | `None` | Runs the guards over a run. Raises `GuardBlocked` on a blocking verdict, prints warnings on stderr. |

### `Callbacks`

```python
Callbacks(before_migrate=None, after_migrate=None, on_error=None)
```

A NamedTuple of three optional functions, given to either migrator.
`before_migrate(connection)` runs before validation and before the advisory
lock. `after_migrate(connection, applied)` runs only when at least one
migration applied. `on_error(connection, migration_id, error)` runs after a
failure; `migration_id` is `None` when the run failed before reaching a
migration. An `on_error` that raises has its own error printed on stderr, and
the run's error still propagates. A `before_migrate` or `after_migrate` that
raises stops the caller.

## Guards

In `sustained.guards`. A guard is
`Callable[[Sequence[str], Dialects], list[Verdict]]`: it reads the statements an
up run would apply and returns one `Verdict(rule, verdict, statement)` per
objection. `verdict` is `BLOCK` (`'block'`) or `WARN` (`'warn'`).

`up()` raises `GuardBlocked` on a blocking verdict, before any statement runs,
and prints warnings on stderr. Callable steps render no SQL, so guards cannot
read them. `down()` does not run guards: a down undoes work the rules already
passed, so `no_drops()` would block every rollback of a create.

| Signature | Returns | Description |
| --- | --- | --- |
| `no_drops()` | `Guard` | Blocks a table, column, view, schema, or database drop. Constraint, index, and key drops pass. |
| `index_must_be_concurrent()` | `Guard` | Blocks `CREATE INDEX` without `CONCURRENTLY`. Postgres only; silent elsewhere. |
| `no_table_rewrite()` | `Guard` | Warns on a column type change, or a NOT NULL with no default for existing rows. |
| `no_lock_without_timeout()` | `Guard` | Blocks a run that alters or drops a table with no `SET lock_timeout` anywhere in it. Postgres only; silent elsewhere. |
| `max_statements(limit)` | `Guard` | Blocks every statement past `limit`. A limit below 1 raises `ValueError`. |
| `run_guards(guards, statements, dialect)` | `list[Verdict]` | Every guard's verdicts, in guard order. |
| `blocking(verdicts)` | `list[Verdict]` | The verdicts that stop a run. |
| `warnings_only(verdicts)` | `list[Verdict]` | The verdicts that only report. |

The scan is textual, like the destructive labels: comments are stripped and
whitespace collapsed, and nothing parses SQL.

## `AsyncMigrator`

In `sustained.aio_migrations`.

```python
AsyncMigrator(adapter, migrations, table='sustained_migrations',
              dialect=Dialects.DEFAULT, tracking_table_options=None,
              rehearsal_table='sustained_rehearsals', guards=None,
              callbacks=None)
```

The same runner on an `AsyncAdapter`. Same tracking table, same `Migration`
objects, same validation rules and refusal messages. Guards and callbacks work
the same way, except that a callback receives the adapter and is awaited when
it returns an awaitable. Properties: `adapter`, `dialect`.

Every method is a coroutine: `applied_records`, `applied`, `pending`,
`status`, `statuses`, `validate`, `repair`, `baseline`, `up`, `rehearse`,
`down`, `down_to`, `record_rehearsal`, `rehearsal_outcome`, `rehearsed`.

Both migrators compute the key the same way, so a row written by one
opens the gate for the other on the same database.

Three methods are absent: **`plan()`, `drift()`, and `script()`**. There is no
async autogeneration and no async offline rendering, so `rehearse()` takes no
`models` here either. Generate against a synchronous connection, then run the
result here.

Callable steps receive the adapter, not a connection, and their return value
is awaited when awaitable.

## Migrations as SQL files

`sustained.migration_files`.

```python
load_migrations(directory, placeholders=None) -> list[Migration]
```

Reads `<id>.up.sql` files, each optionally paired with `<id>.down.sql`, sorted
by id; then `<id>.repeat.sql` repeatables, also sorted by id. Statements split
at line-ending semicolons, so semicolons inside string literals survive, and a
body that contains its own statements, like a trigger or a procedure, does
not.

Raises `ValueError` for: a missing directory; a `.sql` file matching none of
the three patterns; an id with both an up file and a repeat file; a down file
with no up file; and an empty up, down, or repeat file. Files without a `.sql`
extension are ignored, so a README can live alongside the migrations.

```python
substitute_placeholders(text, placeholders, source) -> str
```

Fills `${key}` markers. `$${` escapes a literal `${`. Returns the text
untouched when `placeholders` is `None`. Raises `ValueError` naming the file
for an unknown key or a malformed marker.

Passing a mapping, even an empty one, turns substitution on. Substitution
happens before the checksum is computed, so the checksum covers the SQL that
actually ran.

```python
split_sql_statements(text) -> list[str]
```

Splits on line-ending semicolons and drops whitespace-only and comment-only
pieces.

## Autogeneration internals

`sustained.autogenerate`. `plan()` and `up(models=[...])` sit on top of these.

| Signature | Returns |
| --- | --- |
| `diff_schema(connection, models, dialect=Dialects.DEFAULT, exclude_tables=('sustained_migrations',), renames=None, table_renames=None)` | `SchemaDiff` |
| `autogenerate(connection, models, id, dialect=..., allow_drops=False, ignore_changed_columns=False, exclude_tables=..., renames=None, table_renames=None, type_casts=None, ignore_undeclared=False)` | `Migration` or `None` |
| `introspect_schema(connection, dialect=Dialects.DEFAULT)` | `dict[str, IntrospectedTable]` |
| `await async_introspect_schema(adapter, dialect=Dialects.DEFAULT)` | `dict[str, IntrospectedTable]` |
| `diff_snapshots(before, after)` | `list[str]`, one line per difference between two introspected schemas. Tables and columns only. |
| `normalize_type(raw)` | `str` |
| `normalize_default(raw)` | `str` or `None` |

`diff_schema()` touches nothing and reports every difference, drops included.
`autogenerate()` refuses to generate the lossy ones, and refuses to run at all
while the database holds objects the models do not declare, unless
`allow_drops=True` or `ignore_undeclared=True`. The migrator passes
`ignore_undeclared=True`.

### `SchemaDiff`

| Attribute | Holds |
| --- | --- |
| `missing_tables` | Models with no table |
| `new_columns` | `(model, name, ColumnDef)` |
| `extra_tables` | Table names the models do not declare |
| `extra_columns` | `(table, column)` |
| `changed_columns` | `(table, column, actual, expected)` |
| `new_indexes`, `extra_indexes`, `changed_indexes` | Index differences |
| `constraint_notes` | Differences that are reported but never auto-migrated |

`is_empty()` returns whether there is any difference at all. `summary()`
returns one readable line per difference, marking the destructive ones, or
`schema up to date`.

Primary key, foreign key, column-level UNIQUE, and default differences always
land in `constraint_notes`. They are never migrated automatically.

### What generation refuses

`autogenerate()` raises `ValueError` rather than guess:

- Drops without `allow_drops=True`. The message names the objects and the flag.
- Tightening a column to NOT NULL with no `backfill` or `default`.
- Adding a NOT NULL column with no `backfill` or `default`.
- Adding a primary key or autoincrement column, which ALTER TABLE cannot do.

A migration containing a drop, or a SQLite table rebuild, has no down step,
because neither can be reversed.

## Analysis

`sustained.analysis`, used by `sustained plan`.

| Signature | Returns | Description |
| --- | --- | --- |
| `destructive_statements(statements)` | `list[str]` | The statements that drop a table, drop a column, or truncate. Comments removed, whitespace collapsed. Skips constraint, index, and key drops. |
| `summarize(migration, state)` | `PendingSummary` | One migration reduced to its id, state, repeatable flag, statement count, and destructive statements. |

`PendingSummary(id, state, repeatable, statements, destructive)`. `statements`
is `None` for a callable step, which has no SQL to count.

The scan is textual. A column drop written without the COLUMN keyword, as
MySQL allows, is labelled; so is a drop named inside a string literal. The
label informs the operator. Nothing is blocked, and there is no flag to gate
it.
