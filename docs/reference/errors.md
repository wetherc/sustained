---
layout: default
title: Errors reference
---

Every exception Sustained raises, and the condition behind it.

## The exception tree

```
Exception
├── SustainedError              sustained.exceptions
│   ├── DialectError
│   └── MigrationError
├── RuntimeError
│   └── PoolTimeout             sustained.pool
├── ValueError
├── TypeError
└── AttributeError
```

`SustainedError` is the base for the two errors Sustained defines itself.
`PoolTimeout` sits outside that tree, on `RuntimeError`, so an
`except RuntimeError` around connection handling catches it.

The rest are the standard builtins, raised where a builtin says the right
thing. Sustained does not wrap driver exceptions: a syntax error or a
constraint violation reaches you as your driver's own exception type.

## `DialectError`

A feature the active dialect does not support. Always raised while the
statement builds, never in the database.

The message names what is unsupported and, where there is one, the
alternative:

```
MSSQL does not support RETURNING. Use an OUTPUT clause via raw SQL.
DuckDB has no identity columns. Use a sequence with a DEFAULT expression instead.
QUALIFY is not supported by the 'POSTGRES' dialect. Wrap the window function
in a subquery instead.
```

See [Dialect support](/reference/dialects) for the full matrix.

## `MigrationError`

Migration validation found problems.

Attribute: `problems`, a list of strings. The message is
`Migration validation failed:` followed by one `- ` line per problem.

Raised by `Migrator.up()` and by `Migrator.validate()`, unless you call
`validate(raise_on_problems=False)`, which returns the list instead.

The four problems:

| Problem | Fix |
| --- | --- |
| A migration has a failed attempt on record | Clean up any partial changes by hand, then `repair()` |
| An applied id is not registered with this migrator | Register it, or you are pointing at the wrong migration set |
| A checksum no longer matches | Restore the migration, or `repair()` to accept the new contents |
| A pending migration is ordered before an applied one | `up(allow_out_of_order=True)` |

## `PoolTimeout`

`ConnectionPool` stayed exhausted past its timeout. The message names the
timeout and the pool size. Raise `max_size`, shorten the work holding the
connections, or catch it and shed load.

## `ValueError`

Invalid input the builder can detect. Some are raised by the method you call,
and some when the statement renders. The difference decides where the
traceback points.

**At call time:**

| Condition | Where |
| --- | --- |
| An empty list to `whereIn`, `in_()`, or `insert()` | Filters, writes |
| A negative or non-integer row count | `limit`, `top`, `offset`, `page` |
| `limit()` and `top()` in one query, or either set twice | Paging |
| An operator outside the allowlist | `where`, `having` |
| A `Predicate` passed with an operator or a value | `where`, `having` |
| A subquery in `from_()` with no alias | FROM |
| Rows in a multi-row insert with different columns | `insert` |
| `merge()` or `ignore()` without `onConflict()` | Upserts |
| Both `skip_locked` and `nowait` | `for_update` |
| An index or migration with an empty name, or no columns | Schema, migrations |
| `autoincrement` on a non-integer column, or without `primary_key` | `ColumnDef` |
| `references` without a dot | `ColumnDef` |
| Duplicate migration ids | `Migrator` |
| A repeatable with a down step, or a callable step and no checksum | `Migration` |
| An unresolvable model name in a relation | Relations |
| An unknown relation name | Joins, `withGraphFetched` |

**At render time:**

| Condition |
| --- |
| `UPDATE` or `DELETE` with no `where()` |
| Two different subqueries sharing a CTE alias |
| `merge()` where every inserted column is a conflict column |
| A raw fragment whose `?` count does not match its parameters |
| An INSERT carrying a WHERE clause |
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
| A migration file matching none of the three naming patterns |
| An empty migration file, or a down file with no up file |
| An unknown or malformed `${placeholder}` |

## `TypeError`

A wrong type where the shape matters: a non-`QueryBuilder` CTE or
`insert_from` source, a non-list `using`, a non-string operator, a
non-integer row count.

`bool(predicate)` also raises `TypeError`, on purpose. It turns `a and b` on
two predicates into a loud failure instead of a silent one that keeps only one
side. Use `&` and `|`.

## `AttributeError`

Access to a column a model does not declare. The message lists the declared
set:

```
'Show' does not declare a column named 'titel'. Declared columns: id,
venue_id, title, starts_at, sold_out.
```

Declare `columns`, or `tableColumns`, to turn this on. Without a declaration
every attribute resolves to a column name and a typo reaches the database.

## `RuntimeError`

| Condition | Message names the fix |
| --- | --- |
| No connection resolved | `Model.bind(connection)`, or pass it to `run()` |
| No async adapter resolved | `Model.bind_async(adapter)`, or pass it to `arun()` |
| An `and`/`or` variant as the first condition in a chain | Use the plain form |
| `andOn` or `orOn` as the first join condition | Use `on` |
| A join lambda that added no condition | Add one |
| Nesting `async_transaction()` | There are no async savepoints |
| Using a closed `ConnectionPool` | |
| `to_df()` or `to_arrow()` without pandas or pyarrow | The install command |

## `NotImplementedError`

Async eager loading of a `through` relation. Join it instead, or load it with
a synchronous connection.
