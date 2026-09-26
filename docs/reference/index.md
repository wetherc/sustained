---
layout: default
title: API Reference
description: "API reference for every public Sustained class and method: models, the query builder, predicates, schema types, execution, migrations, the CLI, and errors."
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

You can import some names from the package root, and the rest need their module path.

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

The canonical names are camelCase. A method name matches without regard to case or underscores, so you can also call every method by its snake_case spelling or in any capitalization:

```python
User.query().orderBy('name')     # canonical
User.query().order_by('name')    # the same method
User.query().ORDER_BY('name')    # the same method
```

The rule covers the defined methods such as `select()` and `select_func()`, the `join`, `where`, and `having` families, `groupBy`, `orderBy`, the `on` builder inside a join, and the builder a nested `where` group receives. `whereILike` therefore also resolves as `where_i_like` and `where_ilike`. A name that folds to no method raises `AttributeError`. Use the canonical spelling in new code, because the type stubs describe only that spelling; the other spellings exist so that a port from Objection.js does not fail on capitalization.

## When errors are raised

**At call time.** Sustained checks the arguments themselves: an empty IN list, a negative LIMIT, a `merge()` without `onConflict()`, an unknown comparison operator. These raise `ValueError` or `TypeError` from the method you called.

**At render time**, when `str(query)`, `to_sql()`, or `run()` walks the builder. Sustained checks dialect support and the whole-statement rules: `top()` on Postgres, `RETURNING` on MSSQL, an `UPDATE` with no `WHERE`, a duplicate CTE alias. These raise `DialectError` or `ValueError` from the render call rather than from the method that set them up.

## Reading the signatures

Each entry on these pages opens with its signature on a line of its own, and the text below the signature describes what the call does and what it raises. A `->` on the signature names the return type; a `QueryBuilder` method without one returns the same builder for chaining, except `clone()`, which returns a copy. Tables list the facts that pair up, such as an operator and what it renders, or a dialect and what it refuses.

The signatures are copied from the source, including the defaults. Parameters after `*` are keyword-only.
