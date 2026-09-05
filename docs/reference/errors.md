---
layout: default
title: Errors reference
---

Every exception Sustained raises, and the condition behind it.

## The exception tree

```
Exception
├── SustainedError              sustained.exceptions
│   ├── AmbiguousColumns
│   ├── DialectError
│   ├── GuardBlocked
│   ├── MigrationError
│   └── RehearsalRequired
├── RuntimeError
│   └── PoolTimeout             sustained.pool
├── ValueError
├── TypeError
└── AttributeError
```

`SustainedError` is the base class for the errors Sustained defines itself. `PoolTimeout` sits outside that tree, on `RuntimeError`, so an `except RuntimeError` around connection handling catches a pool timeout.

The rest are the standard builtins, raised where a builtin says the right thing. Sustained does not wrap driver exceptions, so a syntax error or a constraint violation reaches you as your driver's own exception type.

## `AmbiguousColumns`

A result set returns the same column name more than once, which a join over tables that share a column name does. A row is keyed by column name, so one value would replace the other. Sustained raises this before it hydrates the first row, from `run()`, `to_dicts()`, `to_df()`, `to_arrow()`, and their async twins. The `columns` attribute lists the repeated names.

```
This result set returns 'id' more than once, usually from a join over tables
that share a column name. A row keeps one value per name, so the others would
be lost. Alias them in select(), such as select('users.id AS user_id',
'accounts.id AS account_id').
```

## `DialectError`

The query or the schema uses a feature the active dialect does not support. Sustained raises `DialectError` while the statement builds, never in the database.

The message names the unsupported feature and, where there is one, the alternative:

```
MSSQL does not support RETURNING. Use an OUTPUT clause via raw SQL.
DuckDB has no identity columns. Use a sequence with a DEFAULT expression instead.
QUALIFY is not supported by the 'POSTGRES' dialect. Wrap the window function
in a subquery instead.
```

See [Dialect support](/reference/dialects) for the full matrix.

## `MigrationError`

Migration validation found problems.

The `problems` attribute is a list of strings. The message is `Migration validation failed:` followed by one `- ` line per problem.

`Migrator.up()` and `Migrator.validate()` raise it. Call `validate(raise_on_problems=False)` to get the list back instead.

The problems validation reports:

| Problem | Fix |
| --- | --- |
| A migration has a failed attempt on record | Clean up any partial changes by hand, then call `repair()` |
| An applied id is not registered with this migrator | Register the migration, or point the migrator at the right migration set |
| A checksum no longer matches | Restore the migration, or call `repair()` to accept the new contents |
| A pending migration is ordered before an applied one | Call `up(allow_out_of_order=True)` |

## `RehearsalRequired`

A run would apply SQL that removes data, and no passing rehearsal covers that exact set of statements.

`Migrator.up()` and `AsyncMigrator.up()` raise it. Sustained checks the registered migrations before any statement runs. A run with models is checked a second time, against the migration generated from those models, which exists only once the registered migrations have applied. A refusal at that second check leaves the registered migrations applied and lists their ids on the exception's `applied` attribute.

The message names the migration and the statement, then both ways forward:

```
This run removes data, and no rehearsal has proved these statements:
  004_trim  ALTER TABLE users DROP COLUMN legacy
Prove them first: sustained rehearse
Or apply them without proof: sustained migrate --unrehearsed
```

When a rehearsal of the same content ran and failed, the first line reads `The last rehearsal of these statements failed` instead.

The CLI exits 4 on this error. `up(unrehearsed=True)` waives the check and records an `override` row under the same key, which does not open the gate for a later run. A run that only adds does not raise it, and neither does a callable step, which renders no SQL to scan. See [Rehearsal logging and tracking](/schema#rehearsal-logging-and-tracking).

## `GuardBlocked`

A guard returned a blocking verdict on a statement the run would apply.

`Migrator.up()` and `AsyncMigrator.up()` raise it before any statement runs. The `verdicts` attribute lists the blocking verdicts, in the order the guards returned them. The message names each rule and the statement it read:

```
A guard blocked this run:
  no_drops  ALTER TABLE users DROP COLUMN legacy
Fix the statement, or take the rule out of the guard list to run it anyway.
```

No flag waives a guard. `sustained plan` and `sustained migrate` exit 3 when a guard blocks the run. A warning verdict prints on stderr and raises nothing. See [Guards](/schema#guards).

## `PoolTimeout`

`ConnectionPool` stayed exhausted past its timeout. The message names the timeout and the pool size. Raise `max_size`, shorten the work that keeps connections checked out, or catch the timeout and shed load.

## `ValueError`

Invalid input the builder can detect. The method you call raises some of these, and the render raises the rest, which decides where the traceback points.

**At call time:**

| Condition | Where |
| --- | --- |
| An empty list to `whereIn`, `in_()`, or `insert()` | Filters, writes |
| A row count that is negative or not an integer | `limit`, `top`, `offset`, `page` |
| `limit()` and `top()` in one query, or either one set twice | Paging |
| An operator outside the allowlist | `where`, `having` |
| A `Predicate` passed with an operator or a value | `where`, `having` |
| A subquery in `from_()` with no alias | FROM |
| Rows in a multi-row insert with different columns | `insert` |
| `merge()` or `ignore()` without `onConflict()` | Upserts |
| Both `skip_locked` and `nowait` | `for_update` |
| An index or migration with an empty name, or with no columns | Schema, migrations |
| `autoincrement` on a column that is not an integer, or without `primary_key` | `ColumnDef` |
| `references` without a dot | `ColumnDef` |
| Duplicate migration ids | `Migrator` |
| A repeatable with a down step, or a callable step with no checksum | `Migration` |
| A model name in a relation that does not resolve | Relations |
| An unknown relation name | Joins, `withGraphFetched` |

**At render time:**

| Condition |
| --- |
| `UPDATE` or `DELETE` with no `where()` |
| Two different subqueries sharing a CTE alias |
| `merge()` where every inserted column is a conflict column |
| A raw fragment whose `?` count does not match its parameters |
| An INSERT with a WHERE clause |
| A model with no `tableName` in a statement that needs one |
| A string function argument that is neither a plain column path nor a `Literal` |
| `for_update()` combined with a union |

**From migrations:**

| Condition |
| --- |
| An unknown migration target, or a target naming a repeatable |
| Reverting a migration with no down step |
| Rehearsing on a dialect whose schema changes do not roll back |
| Rehearsing on an autocommit connection, or inside a `transaction()` block |
| Generation refusing a drop, a NOT NULL change, or a new primary key column |
| A migration file matching none of the naming patterns |
| An empty migration file, or a down file with no up file |
| A `${placeholder}` that is unknown or malformed |

## `TypeError`

A wrong type in a position that requires a specific one: a CTE or an `insert_from` source that is not a `QueryBuilder`, a `using` value that is not a list, an operator that is not a string, a row count that is not an integer.

`bool(predicate)` also raises `TypeError`, so `a and b` on two predicates fails instead of keeping only one side of the expression. Use `&` and `|`.

## `AttributeError`

You accessed a column the model does not declare. The message lists the declared columns:

```
'Show' does not declare a column named 'titel'. Declared columns: id,
venue_id, title, starts_at, sold_out.
```

Declare `columns`, or `tableColumns`, to turn this check on. Without a declaration, every attribute resolves to a column name and a typo reaches the database.

## `RuntimeError`

| Condition | The message names this fix |
| --- | --- |
| No connection resolved | `Model.bind(connection)`, or pass a connection to `run()` |
| No async adapter resolved | `Model.bind_async(adapter)`, or pass an adapter to `arun()` |
| An `and` or `or` variant as the first condition in a chain | Use the plain form |
| `andOn` or `orOn` as the first join condition | Use `on` |
| A join lambda that added no condition | Add a condition |
| Using a closed `ConnectionPool` | |
| `to_df()` or `to_arrow()` without pandas or pyarrow | The install command |
