---
layout: default
title: Sustained Documentation
---

Sustained is a Python query builder and lightweight ORM inspired by [Objection.js](https://vincit.github.io/objection.js/). You describe a query as chained Python methods; Sustained renders the SQL for your dialect, runs it parameterized, and hydrates the rows into your model classes.

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## What Sustained does

- **Builds SQL programmatically.** Selects, aggregates, window functions, CASE expressions, every join type, CTEs (including recursive), unions, INTERSECT and EXCEPT, subqueries in SELECT, FROM, WHERE, and JOIN clauses.
- **Targets six dialects.** ANSI (default), PostgreSQL, MSSQL, Presto, AWS Athena, and DuckDB. Quoting, placeholders, upsert syntax, LIMIT/OFFSET spelling, and function names all follow the dialect. Unsupported features raise `DialectError` at build time instead of failing in the database.
- **Executes queries safely.** Every statement runs parameterized against any DB-API 2.0 connection or a `ConnectionPool`. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Writes data.** `insert()`, `update()`, `delete()`, upserts through `onConflict()`, `INSERT ... SELECT`, CREATE TABLE AS, and RETURNING.
- **Hydrates results.** Rows become model instances, plain dicts, pandas DataFrames, or pyarrow Tables. Relations eager load with `withGraphFetched()`.
- **Manages schema.** Models declare typed columns and indexes. `Migrator.sync()` diffs the live database against your models, generates a migration, applies it, and `down()` rolls it back. Destructive changes are opt-in.
- **Rehearses migrations.** `sustained rehearse` applies every pending migration, runs the down steps back down, and rolls the whole thing back. A migration that does not run, or does not reverse, says so before it reaches the real schema.
- **Runs async.** The same queries run through driver adapters with `await query.arun()`, including asyncpg and aiosqlite, plus an `AsyncMigrator`.

## What Sustained does not do

These are design decisions, not roadmap gaps. Knowing them up front saves you a search later.

- **No lazy loading or identity map.** A hydrated model instance is a plain object. Accessing an unloaded relation does not trigger a query; use `withGraphFetched()` or a join.
- **No dirty tracking or `save()`.** Writes are explicit statements: `User.query().update({...}).where(...)`. Sustained never writes anything you did not spell out.
- **No connection management beyond the pool.** You create connections (or a factory for the pool); Sustained never reads connection strings or config files.
- **No cross-dialect emulation of missing features.** If MSSQL has no RETURNING, `returning()` raises `DialectError` there. Sustained tells you at build time rather than emulating with extra queries.
- **No guessed migrations.** Autogeneration refuses to invent anything lossy. Drops need `allow_drops=True`, renames need explicit hints, and NOT NULL changes need a `backfill` value. Constraint drift is reported, never silently migrated.
- **No query result caching.** Every `run()` hits the database.

## Finding your way

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
| Look up any method by name | [API Reference](./reference) |

New to Sustained? Read the pages in the order above. Each builds on the one before it.

## Installing

```bash
python3 -m pip install sustained
```

Sustained has no required dependencies. pandas and pyarrow are optional, only needed for `to_df()` and `to_arrow()`.
