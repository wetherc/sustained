---
layout: default
title: API Reference
---

Every public name in Sustained, with its signature, what it returns, and what
it raises. Ten pages, split by the module the names live in.

If you are looking for how to do something rather than what a method takes,
[Recipes](/recipes) is the faster route.

| Page | Covers |
| --- | --- |
| [Model](/reference/model) | `Model`, its class attributes, `create_model`, relation mappings, `RelationType` |
| [QueryBuilder](/reference/query-builder) | Every query method: SELECT, joins, filters, groups, paging, writes, execution |
| [Predicates and expressions](/reference/predicates) | `col`, `Predicate`, `Column`, `Literal`, `Func`, `Subquery`, the function registry |
| [Schema types](/reference/schema) | Column types, `ColumnDef` options, `Index`, `TableOptions`, DDL rendering |
| [Migrations](/reference/migrations) | `Migration`, `Migrator`, `AsyncMigrator`, autogeneration, guards, SQL files, analysis |
| [Execution and pooling](/reference/execution) | Transactions, `ConnectionPool`, async adapters, the statement listener |
| [Command line](/reference/cli) | Every subcommand, flag, exit code, and config-module attribute |
| [Dialect support](/reference/dialects) | What each of the six dialects supports, and what it refuses |
| [Errors](/reference/errors) | Every exception and the condition that raises it |

## What imports from where

Only some names are available from the package root. The rest need their
module path.

```python
# from sustained
from sustained import Model, QueryBuilder, create_model, col
from sustained import Column, ColumnExpr, Literal, Func, Predicate
from sustained import AggregateExpression, WindowExpression, CaseExpression
from sustained import RelationType, RelationMapping, Join
from sustained import Connection, Cursor, Binding, SqlValue, RowValue
from sustained import DialectError, GuardBlocked, MigrationError, RehearsalRequired

# from submodules
from sustained.dialects import Dialects
from sustained.schema import Integer, String, Index, TableOptions, Expression
from sustained.migrations import Migration, Migrator
from sustained.aio_migrations import AsyncMigrator
from sustained.migration_files import load_migrations
from sustained.autogenerate import autogenerate, diff_schema
from sustained.analysis import destructive_statements, summarize
from sustained.guards import no_drops, max_statements, Verdict
from sustained.execution import set_statement_listener
from sustained.pool import ConnectionPool, PoolTimeout
from sustained.aio import DbApiAsyncAdapter, AiosqliteAdapter, AsyncpgAdapter
from sustained.expressions import Subquery
```

`Dialects`, `ConnectionPool`, the async adapters, and the schema types are
deliberately not re-exported at the root.

## Method naming

Canonical names are camelCase, matching Objection.js. Every camelCase method
also answers to its snake_case spelling, because `QueryBuilder.__getattr__`
rewrites `_x` to `X` before it looks the name up:

```python
User.query().orderBy('name')     # canonical
User.query().order_by('name')    # the same method
```

The rewrite only uppercases a letter that follows an underscore. That has one
consequence worth knowing: `whereILike` is spelled `where_i_like` in
snake_case, and `where_ilike` does not resolve.

Method names also match case-insensitively, so `WHERE` and `leftouterjoin`
resolve too. Prefer the canonical spelling; the others exist so a port from
Objection.js does not fail on capitalization.

## When errors are raised

Sustained checks in two places, and the difference decides where a bug shows
up.

**At call time.** Argument shapes: an empty IN list, a negative LIMIT, a
`merge()` without `onConflict()`, an unknown comparison operator. These raise
`ValueError` or `TypeError` from the method you called.

**At render time**, when `str(query)`, `to_sql()`, or `run()` walks the
builder. Dialect support and whole-statement rules: `top()` on Postgres,
`RETURNING` on MSSQL, an `UPDATE` with no `WHERE`, a duplicate CTE alias.
These raise `DialectError` or `ValueError` from the render call, not from the
method that set them up.

Nothing about dialect support is checked in the database. A feature the active
dialect lacks fails while you are building the statement.

## Reading the signatures

Signatures are copied from the source, including defaults. Parameters after
`*` are keyword-only. `Self` is written as the class name, because these
methods return the same builder for chaining, not a copy. The one exception is
`clone()`, which does return a copy.
