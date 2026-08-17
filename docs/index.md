---
layout: default
title: Sustained Documentation
---

Sustained is a Python query builder, lightweight ORM, and schema migration tool, originally inspired by [Objection.js](https://vincit.github.io/objection.js/). You define one set of model classes to describe your tables, and Sustained builds and runs the queries against them, and keeps the schema itself in step.

The syntax will look familiar if you have worked with Objection, Kysely, or even knex before:

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## Managing queries through Sustained

With Sustained, you can:

- **Build SQL programmatically.** Selects, aggregates, window functions, CASE expressions, every join type, CTEs (including recursive), unions, INTERSECT and EXCEPT, subqueries in SELECT, FROM, WHERE, and JOIN clauses.
- **Target seven dialects.** ANSI (default), PostgreSQL, MySQL and MariaDB, MSSQL, Presto, AWS Athena, and DuckDB. Quoting, placeholders, upsert syntax, LIMIT/OFFSET spelling, and function names all follow the dialect. Unsupported features raise `DialectError` at build time instead of failing in the database. Migrating queries between dialects is a one-line change.
- **Execute queries safely.** Every statement runs parameterized against any DB-API 2.0 connection or a `ConnectionPool`. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Write data.** `insert()`, `update()`, `delete()`, upserts through `onConflict()`, `INSERT ... SELECT`, CREATE TABLE AS, and RETURNING.
- **Hydrate results.** Rows become model instances, plain dicts, pandas DataFrames, or pyarrow Tables. Relations eager load with `withGraphFetched()`. A type checker reads `Show.query().run()` as `List[Show]`.
- **Run queries async.** The same queries run through driver adapters with `await query.arun()`, including asyncpg and aiosqlite.

## Schema management with Sustained

Sustained also provides strong support for database schema change management, to allow you easily and reliably test schema changes safely, evolve your database schema, and easily roll back migrations. These features are discussed in detail at [Schema and Migrations](./schema).

With Sustained, schema migrations are:

- **Generated from your models.** `Migrator.up(models=[...])` diffs the live database against your models, generates the migration, records it, and applies it. Run it again after a model change and only the difference is applied. `down()` rolls it back.
- **Rehearsed before they land.** `sustained rehearse` applies every pending migration, runs the downgrade steps to test the revert plan, and rolls the whole thing back. A migration that does not run, or does not reverse, says so before it reaches the real schema. A config module can send the rehearsal to a scratch database instead.
- **Planned in one screen.** `sustained plan` shows your pending migrations, outstanding problems that `validate` would report, and any gap between your models and the database's current state.
- **Checked, not trusted.** Sustained manages a per-database tracking table that holds a sequence number, a SHA-256 checksum, an apply timestamp, execution time, and a success flag per migration. `validate` refuses a run when a migration was edited after it ran, arrives out of order, or left a failed attempt behind. `repair` will delete failed runs from the tracking table and update script checksums after manual corrections.
- **Gated by custom safeguards.** Guards can be built-in functions (`no_drops()`, `index_must_be_concurrent()`, `max_statements(n)`) or can be a custom function you write. These read every statement of a migration that would run and block the deployment if any of the rule checks fail.
- **Safe by default.** Drops need explicit `allow_drops=True`, renames need explicit hints, NOT NULL changes need a `default` or `backfill`. Destructive changes will never run by default.
- **Written your way.** Migrations can be Python `Migration` objects, `<id>.up.sql` and `<id>.down.sql` files with `${placeholders}`, or `<id>.repeat.sql` files for views and seed data, which re-run whenever their contents change.
- **Ready for deploys.** The `sustained` console script runs `plan`, `status`, `rehearse`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline`, with exit codes for pipelines and `before_migrate`, `after_migrate`, and `on_error` callbacks around a run. Concurrent deploys queue on an advisory lock. `baseline` adopts a database that already matches. `script('up')` renders the SQL for a DBA instead of running it. `AsyncMigrator` does all of it on an async adapter.


## Finding your way

**New here?** [Getting Started](./getting-started) builds a working application in one sitting: a schema that Sustained creates, queries that join across tables, a scripted generated migration, and the same migrations run interactively from the shell. It needs nothing but SQLite to run.

**Have a task in mind?** [Recipes](./recipes) provides concrete examples of how to complete specific, common tasks with Sustained.

**Looking up a method?** The [API Reference](./reference/) provides a comprehensive reference for all of Sustained's public API methods.

The guides each explain one area in depth, and they build on each other in this order:

| You want to | Read |
| --- | --- |
| Map classes to tables, catch column typos | [Models](./models) |
| Build a SELECT: columns, functions, CTEs, unions, pagination | [Queries](./queries) |
| Target a specific engine, pick a driver | [SQL Dialects](./dialects) |
| Filter rows: where methods and typed predicates | [Filtering](./filtering) |
| Aggregate and filter groups | [Grouping](./grouping) |
| Join tables and define relations | [Relations and Joins](./relations) |
| Run queries, write data, transactions, pooling, async | [Executing Queries](./executing) |
| Create tables, rehearse, generate and roll back migrations | [Schema and Migrations](./schema) |

Released versions are listed in the [Changelog](./changelog).

## Installing

```bash
python3 -m pip install sustained
```

Sustained has no required dependencies. pandas and pyarrow are optional, only needed for `to_df()` and `to_arrow()`.
