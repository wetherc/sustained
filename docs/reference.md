---
layout: default
title: API Reference
---

Every public method, grouped by task. Each table links back to the guide that shows the method in context. Canonical names are camelCase, matching Objection.js; every camelCase method also answers to its snake_case spelling (`orderBy` / `order_by`).

## Starting points

```python
from sustained import Model, col

class User(Model):
    tableName = 'users'

query = User.query()        # a new QueryBuilder for the model's table
```

| Method | What it does |
| --- | --- |
| `Model.query()` | Starts a new `QueryBuilder` on the model's table. |
| `create_model(name, table_name, mappings=None, table_schema=None, database=None, columns=None)` | Builds a model class at runtime. |
| `Model.set_dialect(dialect)` | Sets the SQL dialect for the model's queries: `Dialects.DEFAULT`, `POSTGRES`, `MSSQL`, `PRESTO`, `ATHENA`, or `DUCKDB`. |
| `Model.bind(connection)` / `Model.unbind()` | Attaches or removes a DB-API connection or `ConnectionPool` for execution. |
| `Model.bind_async(adapter)` / `Model.unbind_async()` | Attaches or removes an async adapter. |

Guide: [Models](./models)

## Selecting

| Method | What it does |
| --- | --- |
| `select(*columns)` | Names the output columns. Accepts strings, `'col AS alias'` shorthand, `Model.column` references, and expression objects. Defaults to `*`. |
| `distinct()` | Adds DISTINCT. |
| `distinctOn(*columns)` | Postgres/DuckDB `DISTINCT ON (...)`; other dialects raise `DialectError`. |
| `from_(source, alias=None)` | Overrides the FROM source with a table name or a `QueryBuilder` subquery (alias required for subqueries). |
| `count(column='*', alias=None)`, `sum()`, `avg()`, `min()`, `max()` | Aggregate shortcuts. |
| `select_func(name, *args, alias=None)` | Any SQL function. String arguments are column references; wrap literals in `Literal()`. Registered functions validate against the dialect. |
| `lower()`, `upper()`, `coalesce()`, `concat()`, `substring()`, `trim()`, `length()`, `round()`, `abs()`, `ceiling()`, `floor()`, `mod()`, `now()`, `getdate()` | Fluent shortcuts for registered functions. Each is `select_func` under the hood, so `q.coalesce('nick', 'name', alias='d')` works. Names translate per dialect: `now()` renders `GETDATE()` on MSSQL, `length()` renders `LEN()`. A function with no spelling on the active dialect raises `DialectError`. |
| `select_window(func, alias, partition_by=None, order_by=None, args=None, frame=None)` | A window function with PARTITION BY, ORDER BY, function arguments, and a frame clause. |
| `select_case(alias, default, when_clauses)` | A CASE expression. Results are literals unless wrapped in `Column()`. |
| `with_(alias, query, recursive=False)` | Adds a CTE. Renders `WITH RECURSIVE` when asked, plain `WITH` on MSSQL. |

Guide: [Queries](./queries)

## Filtering

| Method | What it does |
| --- | --- |
| `where(column, op, value)` / `andWhere` / `orWhere` | A comparison predicate. Operators come from an allowlist; `None` with `=` or `!=` renders IS NULL / IS NOT NULL. Also accepts a `Predicate` or a lambda for a parenthesized group. |
| `Model.c.<column>` and `col(path)` | Typed column references. Python operators build `Predicate` objects, combined with `&`, `|`, `~`. |
| `.like()`, `.not_like()`, `.ilike()`, `.in_()`, `.not_in()`, `.between()`, `.not_between()`, `.is_null()`, `.not_null()` | Predicate methods on a typed column. |
| `whereRaw(sql, params)` | A raw predicate with `?` value markers; values still parameterize. |
| `whereIn(column, values_or_query)` / `whereNotIn` | IN lists or subqueries. Empty lists raise `ValueError`. |
| `whereNull(column)` / `whereNotNull(column)` | IS NULL / IS NOT NULL. |
| `whereBetween(column, low, high)` / `whereNotBetween` | BETWEEN ranges. |
| `whereLike(column, pattern)` / `whereILike` | LIKE and case-insensitive LIKE. ILIKE is native on Postgres, `LOWER() LIKE LOWER()` elsewhere. |
| `whereExists(query_or_callable)` / `whereNotExists` | EXISTS subqueries. |
| `QueryBuilder.raw(sql)` | A raw fragment that bypasses quoting and validation. |

Every `where` method has `and`/`or` prefixed forms (`andWhereIn`, `orWhereBetween`, ...). The first call in a chain must be plain `where`.

Guide: [Filtering](./filtering)

## Grouping and window filters

| Method | What it does |
| --- | --- |
| `groupBy(*columns)` | GROUP BY. |
| `groupByRollup(*columns)` / `groupByCube(*columns)` / `groupByGroupingSets(*tuples)` | Subtotal and multi-grain aggregation. |
| `having(column, op, value)` and the full `having*` family | Filters after aggregation. Mirrors every `where` variant, including `havingRaw`, `havingIn`, `havingLike`, `havingNull`, `havingBetween`, and lambda groups. |
| `qualify(condition)` | Filters on window function results (DuckDB). |

Guide: [Grouping](./grouping)

## Joining

| Method | What it does |
| --- | --- |
| `join(table, col1, op, col2)` and `innerJoin`, `leftJoin`, `leftOuterJoin`, `rightJoin`, `rightOuterJoin`, `fullOuterJoin`, `crossJoin` | Raw joins by ON condition, `using=[...]` column list, or a lambda receiving a `JoinBuilder`. |
| `joinRelated(name, alias=None)` and the same prefixed family | Joins a relation declared in `relationMappings`. Through relations join the intermediate table automatically. |
| `JoinBuilder.on(c1, op, c2)` / `.andOn()` / `.orOn()` | Compound ON conditions inside a join lambda. The right side can be a `QueryBuilder`. |

Guide: [Relations and Joins](./relations)

## Ordering, paging, combining

| Method | What it does |
| --- | --- |
| `orderBy(column, direction='asc')` | Sorts. Chain calls for multiple columns. |
| `limit(n)` / `offset(n)` | Row cap and skip. On MSSQL these compile to OFFSET/FETCH and require `orderBy()`. |
| `top(n)` | SQL Server TOP; raises `DialectError` elsewhere. Mutually exclusive with `limit()`. |
| `page(page, page_size)` | LIMIT and OFFSET from a zero-based page number. |
| `cursor_page(column, page_size, after=None)` | Keyset pagination; avoids OFFSET scan cost on large tables. |
| `total(connection=None)` | SELECT COUNT(*) over the query with ordering and paging stripped. |
| `union(*queries)` / `unionAll(*queries)` | Combines result sets. Member CTEs hoist to the top. |
| `intersect(*queries)` / `except_(*queries)` | Set intersection and difference. |
| `for_update(skip_locked=False, nowait=False)` | Row locking on Postgres. |
| `clone()` | Copies the builder so a shared base query can branch. |

Guide: [Queries](./queries)

## Writing data

| Method | What it does |
| --- | --- |
| `insert(row_or_rows)` | INSERT from a dict or list of dicts. Multi-row inserts use the driver's `executemany()` when there is no RETURNING. |
| `insert_from(columns, query)` | INSERT ... SELECT. |
| `update(values)` | UPDATE. Raises without a `where()`. |
| `delete()` | DELETE. Raises without a `where()`. |
| `onConflict(*columns).merge(columns=None)` / `.ignore()` | Upserts. ON CONFLICT on Postgres/SQLite/DuckDB, MERGE on MSSQL, `DialectError` on Presto. |
| `returning(*columns)` | RETURNING clause; the statement returns dicts instead of a row count. MSSQL and Presto raise. |
| `create_table_as(name, temporary=False)` | CREATE TABLE AS from a SELECT. MSSQL raises. |

Guide: [Executing Queries](./executing)

## Executing

| Method | What it does |
| --- | --- |
| `str(query)` | The SQL with values inlined as literals, for logging and debugging. |
| `to_sql()` | `(sql, params)` with dialect placeholders, ready for any DB-API cursor. |
| `run(connection=None)` | Executes. SELECTs return hydrated model instances; writes commit and return the affected row count. |
| `first(connection=None)` | Runs with LIMIT 1 and returns one instance or `None`. |
| `to_dicts()` / `to_df()` / `to_arrow()` | Rows as dicts, a pandas DataFrame, or a pyarrow Table. |
| `withGraphFetched(*relations)` | Eager loads relations, one extra query each. |
| `explain(analyze=False)` | Returns the query plan. `analyze=True` executes the statement. MSSQL raises. |
| `arun()` / `afirst()` / `ato_dicts()` | Async equivalents through the bound adapter. |
| `Model.transaction(connection=None)` | A commit-or-rollback context; nested blocks use savepoints. |
| `Model.async_transaction(adapter=None)` | The async equivalent; does not nest. |

Guide: [Executing Queries](./executing)

## Pooling, logging, async adapters

| Name | What it does |
| --- | --- |
| `ConnectionPool(factory, max_size=..., timeout=...)` | Thread-safe, lazily filled pool. Bind it like a connection. `connection()`, `acquire_raw()`, `release()`, `close()`, `size`. Exhaustion raises `PoolTimeout`. |
| `sustained.execution.set_statement_listener(fn)` | Observer called with SQL, parameters, and duration for every executed statement. `None` removes it. |
| `DbApiAsyncAdapter(connection)` | Runs any synchronous DB-API connection in a worker thread. |
| `AiosqliteAdapter(connection)` | Native aiosqlite. |
| `AsyncpgAdapter(connection)` | Native asyncpg; converts `%s` placeholders to `$1..$n`. |

All adapters live in `sustained.aio` and share `fetch`, `execute`, `executemany`, `commit`, and `rollback`.

## Schema definition

| Name | What it does |
| --- | --- |
| `Integer`, `BigInteger`, `String(length)`, `Text`, `Boolean`, `Float`, `Numeric(precision, scale)`, `Date`, `Timestamp`, `Json` | Typed column factories for `tableColumns`. Types map per dialect. |
| ColumnDef options | `primary_key`, `autoincrement`, `nullable`, `unique`, `default` (literal or raw `Expression`), `references='table.column'`, `backfill` (value for NOT NULL migrations). |
| `Index(name, *columns, unique=False)` | A named index, declared in the model's `indexes` list. |
| `TableOptions(location=None, partitioned_by=None, properties=None)` | Storage clauses for engines that need them, declared as the model's `tableOptions`. Athena renders PARTITIONED BY, LOCATION, and TBLPROPERTIES; other dialects raise. |
| `Model.create_table_sql(if_not_exists=False)` | The CREATE TABLE statement. |
| `Model.create_table_statements()` | CREATE TABLE plus CREATE INDEX statements. |
| `Model.create_table(connection=None)` | Executes them. |
| `Model.drop_table_sql(if_exists=True)` / `Model.drop_table()` | DROP TABLE. |

Guide: [Schema and Migrations](./schema)

## Migrations

| Name | What it does |
| --- | --- |
| `Migration(id, up, down=None)` | One schema change. Steps are a SQL string, a list of statements, or a callable receiving the connection. |
| `Migrator(connection, migrations, table='sustained_migrations', dialect=..., tracking_table_options=None)` | Applies and reverts migrations with a tracking table, one transaction per migration. Engines without transactions (Athena) run steps bare; `tracking_table_options` supplies the tracking table's storage clauses there. |
| `migrator.up(target=None)` | Applies pending migrations, optionally stopping after a target id. |
| `migrator.down(steps=1)` / `migrator.down_to(id)` | Reverts newest-first. |
| `migrator.sync(models, ...)` | Diffs the database against the models, generates a migration, applies it. Accepts `allow_drops`, `ignore_changed_columns`, `renames`, `table_renames`, `type_casts`, `migration_id`. |
| `migrator.status()` / `applied()` / `pending()` | What has and has not run. |
| `migrator.script(direction)` | Renders the SQL a run would execute, including tracking bookkeeping, without executing. |
| `create_table_migration(model)` | A create/drop migration pair derived from a model. |
| `migration_sql(migration, direction)` | One migration's statements for offline review. |
| `AsyncMigrator` | The same runner on an `AsyncAdapter`, in `sustained.aio_migrations`. |
| `diff_schema(connection, models, ...)` | The schema difference as a `SchemaDiff` with a readable `summary()`. Touches nothing. |
| `autogenerate(connection, models, id, ...)` | Builds a `Migration` from the diff, or `None` when the schema is current. |

Guide: [Schema and Migrations](./schema)

## Expressions

All in `sustained.expressions`; the common ones re-export from `sustained`.

| Name | What it does |
| --- | --- |
| `col(path)` | A typed column reference outside a model. |
| `Predicate` | A composable condition from typed comparisons. Raises `TypeError` in boolean context to catch `and`/`or` misuse. |
| `Column(text)` | Marks a string as a column reference where a literal would be assumed. |
| `Literal(value)` | Marks a string as a literal where a column would be assumed. |
| `Func(name, *args, alias=None)` | A function call expression for `select()`. |
| `AggregateExpression`, `WindowExpression`, `CaseExpression` | Expression objects behind the fluent aggregate, window, and CASE methods. |
| `Subquery(query, alias)` | Embeds a `QueryBuilder` in a SELECT list or a join. |

## Errors

| Exception | When |
| --- | --- |
| `DialectError` | A feature the active dialect does not support, raised at build time. |
| `ValueError` | Invalid input the builder can detect: unknown operators, empty IN lists, unfiltered writes, duplicate CTE aliases, bad migration targets. |
| `PoolTimeout` (`sustained.pool`) | The pool stayed exhausted past its timeout. |
| `AttributeError` | Access to a column not in a model's declared `columns`. |
