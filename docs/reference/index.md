---
layout: default
title: API Reference
---

Technical reference material covering all public Sustained classes and methods.

If you are looking for how to do something specific, [Recipes](/recipes) is the faster route.

| Page | Covers |
| --- | --- |
| [Model](/reference/model) | `Model`, its class attributes, `create_model`, relation mappings, `RelationType` |
| [QueryBuilder](/reference/query-builder) | Every query method: SELECT, joins, filters, groups, paging, writes, execution |
| [Predicates and expressions](/reference/predicates) | `col`, `Predicate`, `Column`, `Literal`, `Func`, `Subquery`, the function registry |
| [Schema types](/reference/schema) | Column types, `ColumnDef` options, `Enum`, `Check`, `ForeignKey`, `Index`, `TableOptions`, DDL rendering |
| [Migrations](/reference/migrations) | `Migration`, `Migrator`, `AsyncMigrator`, ddl steps, autogeneration, guards, SQL files, analysis |
| [Execution and pooling](/reference/execution) | Transactions, `ConnectionPool`, async adapters, the statement listener |
| [Command line](/reference/cli) | Every subcommand, flag, exit code, and config-module attribute |
| [Dialect support](/reference/dialects) | What each dialect supports, and what it refuses |
| [Errors](/reference/errors) | Every exception and the condition that raises it |

## What imports from where

Some names are available from the package root. The rest need their module path.

```python
# from sustained
from sustained import Model, QueryBuilder, create_model, col
from sustained import Column, ColumnExpr, Literal, Func, Predicate
from sustained import AggregateExpression, WindowExpression, CaseExpression
from sustained import RelationType, RelationMapping, Join
from sustained import Connection, Cursor, Binding, SqlValue, RowValue
from sustained import DialectError, GuardBlocked, MigrationError, RehearsalRequired
from sustained import AmbiguousColumns

# from submodules
from sustained.dialects import Dialects
from sustained.schema import Integer, String, Enum, Check, ForeignKey
from sustained.schema import Index, TableOptions, Expression
from sustained.migrations import Migration, Migrator
from sustained import ddl
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

The package root does not re-export `Dialects`, `ConnectionPool`, the async adapters, or the schema types.

## Method naming

The canonical names are camelCase. Every camelCase method also answers to its snake_case spelling, because `QueryBuilder.__getattr__` rewrites `_x` to `X` before it looks the name up:

```python
User.query().orderBy('name')     # canonical
User.query().order_by('name')    # the same method
```

The rewrite uppercases only a letter that follows an underscore. So `whereILike` is spelled `where_i_like` in snake_case, and `where_ilike` does not resolve.

Method names also match case-insensitively, so `WHERE` and `leftouterjoin` resolve as well. Use the canonical spelling. The other spellings exist so that a port from Objection.js does not fail on capitalization.

## When errors are raised

**At call time.** Sustained checks the argument shapes: an empty IN list, a negative LIMIT, a `merge()` without `onConflict()`, an unknown comparison operator. These raise `ValueError` or `TypeError` from the method you called.

**At render time**, when `str(query)`, `to_sql()`, or `run()` walks the builder. Sustained checks dialect support and the whole-statement rules: `top()` on Postgres, `RETURNING` on MSSQL, an `UPDATE` with no `WHERE`, a duplicate CTE alias. These raise `DialectError` or `ValueError` from the render call rather than from the method that set them up.

Sustained checks dialect support while you build the statement, so a feature the active dialect lacks fails before the statement reaches the database.

## Reading the signatures

Each entry on these pages opens with its signature on a line of its own, and the text below the signature describes what the call does and what it raises. A `->` on the signature names the return type; a `QueryBuilder` method without one returns the same builder for chaining. `clone()` is the exception: it returns a copy. Tables carry the facts that pair up, such as an operator and what it renders, or a dialect and what it refuses.

The signatures are copied from the source, including the defaults. Parameters after `*` are keyword-only.
