---
layout: default
title: Sustained Documentation
---

Sustained is a Python query builder, lightweight ORM, and schema migration tool, inspired by [Objection.js](https://vincit.github.io/objection.js/). One set of model classes describes your tables. Sustained builds and runs the queries against them, and keeps the schema itself in step.

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down ok
rehearsed 004_trim      up ok, down ok
rollback complete, database unchanged
```

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## Migrations first

A migration is the part of a schema change you cannot take back once it has run, so Sustained spends most of its safety budget there. See [Schema and Migrations](./schema) for all of it.

- **Generated from your models.** `Migrator.up(models=[...])` diffs the live database against your models, generates the migration, records it, and applies it. Run it again after a model change and only the difference is applied. `down()` rolls it back.
- **Rehearsed before they land.** `sustained rehearse` applies every pending migration, runs the down steps back down, and rolls the whole thing back. A migration that does not run, or does not reverse, says so before it reaches the real schema. A config module can send the rehearsal to a scratch database instead.
- **Planned in one screen.** `sustained plan` merges pending migrations, the problems `validate` would report, and the gap between your models and the database, labels destructive statements, and exits 2 when work is waiting. `status`, `validate`, and `plan` all take `--json`.
- **Checked, not trusted.** The tracking table holds a sequence number, a SHA-256 checksum, an apply timestamp, execution time, and a success flag per migration. `validate()` refuses a run when a migration was edited after it ran, arrives out of order, or left a failed attempt behind. `repair()` fixes the bookkeeping.
- **Safe by refusal.** Drops need `allow_drops=True`, renames need explicit hints, NOT NULL changes need a `default` or `backfill`. Constraint drift is reported, never silently migrated.
- **Written your way.** Migrations can be Python `Migration` objects, `<id>.up.sql` and `<id>.down.sql` files with `${placeholders}`, or `<id>.repeat.sql` files for views and seed data, which re-run whenever their contents change. Hand-written and generated migrations share one ordered list and one tracking table.
- **Ready for deploys.** The `sustained` console script runs `plan`, `status`, `rehearse`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline`, with exit codes for pipelines and `before_migrate`, `after_migrate`, and `on_error` callbacks around a run. Concurrent deploys queue on an advisory lock. `baseline` adopts a database that already matches. `script('up')` renders the SQL for a DBA instead of running it. `AsyncMigrator` does all of it on an async adapter.

## What else Sustained does

- **Builds SQL programmatically.** Selects, aggregates, window functions, CASE expressions, every join type, CTEs (including recursive), unions, INTERSECT and EXCEPT, subqueries in SELECT, FROM, WHERE, and JOIN clauses.
- **Targets six dialects.** ANSI (default), PostgreSQL, MSSQL, Presto, AWS Athena, and DuckDB. Quoting, placeholders, upsert syntax, LIMIT/OFFSET spelling, and function names all follow the dialect. Unsupported features raise `DialectError` at build time instead of failing in the database.
- **Executes queries safely.** Every statement runs parameterized against any DB-API 2.0 connection or a `ConnectionPool`. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Writes data.** `insert()`, `update()`, `delete()`, upserts through `onConflict()`, `INSERT ... SELECT`, CREATE TABLE AS, and RETURNING.
- **Hydrates results.** Rows become model instances, plain dicts, pandas DataFrames, or pyarrow Tables. Relations eager load with `withGraphFetched()`.
- **Runs async.** The same queries run through driver adapters with `await query.arun()`, including asyncpg and aiosqlite.

## What Sustained does not do

These are design decisions, not roadmap gaps. Knowing them up front saves you a search later.

- **No lazy loading or identity map.** A hydrated model instance is a plain object. Accessing an unloaded relation does not trigger a query; use `withGraphFetched()` or a join.
- **No dirty tracking or `save()`.** Writes are explicit statements: `User.query().update({...}).where(...)`. Sustained never writes anything you did not spell out.
- **No connection management beyond the pool.** You create connections (or a factory for the pool); Sustained never reads connection strings or config files.
- **No cross-dialect emulation of missing features.** If MSSQL has no RETURNING, `returning()` raises `DialectError` there. Sustained tells you at build time rather than emulating with extra queries.
- **No guessed migrations.** Autogeneration refuses to invent anything lossy. Drops need `allow_drops=True`, renames need explicit hints, and NOT NULL changes need a `backfill` value. Constraint drift is reported, never silently migrated.
- **No query result caching.** Every `run()` hits the database.

## Finding your way

**New here?** [Getting Started](./getting-started) builds a working
application in one sitting: a schema Sustained creates, queries that join
across tables, a generated migration, and the same migrations run from the
shell. It needs nothing but SQLite from the standard library.

**Have a task in mind?** [Recipes](./recipes) is a task, the code that does it,
and the thing that will bite you, about fifty times over.

**Looking up a method?** The [API Reference](./reference/) gives every public
name its signature, its return type, and the conditions that raise.

The guides sit between those. Each explains one area in depth, and they build
on each other in this order:

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
