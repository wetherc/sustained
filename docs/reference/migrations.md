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
         dialect=Dialects.DEFAULT, tracking_table_options=None)
```

Applies and reverts migrations, records them in a tracking table, and runs
each inside a transaction. Duplicate ids raise `ValueError`.

Property: `connection`.

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
| `up(target=None, validate=True, allow_out_of_order=False)` | `list[str]` | Validates, then applies pending migrations in order. `target` stops after that id and skips the repeatables. |
| `down(steps=1)` | `list[str]` | Reverts newest-first. Never touches repeatables. |
| `down_to(target)` | `list[str]` | Reverts until `target` is the newest applied. |
| `baseline(target)` | `list[str]` | Records migrations up to and including `target` as applied, without running them. Also records every repeatable at its current checksum. |

`up` raises `MigrationError` when validation finds problems, and `ValueError`
for an unknown target or a target naming a repeatable. `down` and `down_to`
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
| `plan(models, ...)` | `Migration` or `None` | The migration `sync()` would generate. Records nothing, applies nothing. `None` when the schema is current. |
| `sync(models, ...)` | `list[str]` | Generates, registers, and applies it. |

Both take the same options:

| Option | Default | Meaning |
| --- | --- | --- |
| `allow_drops` | `False` | Generate drops for tables and columns the models do not declare. |
| `ignore_changed_columns` | `False` | Skip type and nullability differences entirely. |
| `migration_id` | generated | The id. Defaults to `auto_<UTC timestamp>`. |
| `renames` | `None` | `{'table.old': 'new'}`, so a rename is a rename and not a drop plus an add. |
| `table_renames` | `None` | `{'old': 'new'}`. |
| `type_casts` | `None` | `{'table.col': 'col::integer'}`, a `USING` hint. Postgres only. |

Pass every model you manage. Both compare the whole database against the whole
list, and a table missing from the list looks like one you want dropped. The
tracking table is always excluded.

### Rehearsing

```python
rehearse(scratch=False) -> list[RehearsalResult]
```

Applies every pending migration, runs the down steps back down, and rolls the
whole thing back. Returns `[]` when nothing is pending.

Refuses, with `ValueError`, when:

- The dialect is not one whose schema changes roll back. Only the default
  dialect, Postgres, and DuckDB qualify. `scratch=True` waives this for a
  connection to a database you can throw away.
- The connection is in autocommit mode.
- The call is inside an open `transaction()` block, because its rollback would
  take the caller's work back too.

The check reads the declared dialect, not the engine. A config that leaves the
dialect unset while pointing at, say, MySQL would rehearse for real.

### Rendering without running

```python
script(direction='up') -> str
```

Every statement a run would execute, tracking bookkeeping included, as text.
Any direction other than `up` or `down` raises `ValueError`.

## Result types

`AppliedRecord(id, seq, checksum, success)` holds one tracking row.

`RehearsalResult(id, up_ok, down_ok, error)` holds what a rehearsal proved
about one migration. `down_ok` is `None` when nothing was proved, and `error` then
says why: `no down step`, `no down step (repeatable)`, `down not reached: ...`,
or `down not rehearsed: the run stopped`.

## The tracking table

Six columns, named by default `sustained_migrations`:

| Column | Type | Holds |
| --- | --- | --- |
| `id` | `VARCHAR(255)` primary key | The migration id |
| `seq` | `INTEGER` | The apply order |
| `checksum` | `VARCHAR(64)` | SHA-256 of the up statements |
| `applied_at` | `TEXT` not null | When it ran |
| `execution_ms` | `INTEGER` | How long it took. Null for a baselined row |
| `success` | `BOOLEAN` not null | Whether it finished |

On Athena the same six columns are all plain and nullable, because Athena
enforces no constraints. Tracking tables written by earlier versions, holding
only `id` and `applied_at`, upgrade in place on first use.

## Module functions

| Signature | Returns | Description |
| --- | --- | --- |
| `migration_checksum(migration)` | `str` or `None` | The checksum validation compares. `None` for a callable step with no explicit checksum. |
| `create_table_migration(model)` | `Migration` | A create/drop pair derived from a model. |
| `migration_sql(migration, direction='up')` | `list[str]` | One migration's statements, for offline review. A callable step renders as a comment. Raises `ValueError` when that step is `None`. |

## `AsyncMigrator`

In `sustained.aio_migrations`.

```python
AsyncMigrator(adapter, migrations, table='sustained_migrations',
              dialect=Dialects.DEFAULT, tracking_table_options=None)
```

The same runner on an `AsyncAdapter`. Same tracking table, same `Migration`
objects, same validation rules and refusal messages. Property: `adapter`.

Every method is a coroutine: `applied_records`, `applied`, `pending`,
`status`, `statuses`, `validate`, `repair`, `baseline`, `up`, `rehearse`,
`down`, `down_to`.

Three methods are absent: **`plan()`, `sync()`, and `script()`**. There is no
async autogeneration and no async offline rendering. Generate against a
synchronous connection, then run the result here.

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

`sustained.autogenerate`. `plan()` and `sync()` sit on top of these.

| Signature | Returns |
| --- | --- |
| `diff_schema(connection, models, dialect=Dialects.DEFAULT, exclude_tables=('sustained_migrations',), renames=None, table_renames=None)` | `SchemaDiff` |
| `autogenerate(connection, models, id, dialect=..., allow_drops=False, ignore_changed_columns=False, exclude_tables=..., renames=None, table_renames=None, type_casts=None)` | `Migration` or `None` |
| `introspect_schema(connection, dialect=Dialects.DEFAULT)` | `dict[str, IntrospectedTable]` |
| `normalize_type(raw)` | `str` |
| `normalize_default(raw)` | `str` or `None` |

`diff_schema()` touches nothing and reports every difference, drops included.
`autogenerate()` refuses to generate the lossy ones.

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
