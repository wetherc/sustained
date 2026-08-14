# Changelog

## 2.1.0 (2026-08-14)

### Added

- Typed predicates: `Model.c.age > 21` and `col()` build composable `Predicate` objects combinable with `&`, `|`, `~`; accepted by `where()` and `having()`.
- `whereRaw()` / `havingRaw()`: raw predicates with `?` value markers that parameterize like every other clause.
- `Model.transaction()` context manager with savepoint nesting; `run()` defers commits inside a transaction.
- `set_statement_listener()` observer with SQL, parameters, and duration for every executed statement.
- Upserts: `insert().onConflict(cols).merge()` / `.ignore()`. ON CONFLICT on Postgres/SQLite/DuckDB, MERGE on MSSQL, DialectError on Presto.
- `insert_from()` (INSERT ... SELECT) and `create_table_as()` (CTAS; MSSQL raises).
- Multi-row inserts execute through the driver's `executemany()` when there is no RETURNING clause.
- Result formats: `to_dicts()`, `to_df()` (pandas optional), `to_arrow()` (pyarrow optional).
- DuckDB dialect: quoting, native ILIKE, qmark placeholders, upserts, RETURNING, CTAS, QUALIFY.
- Recursive CTEs via `with_(..., recursive=True)`; MSSQL renders plain WITH.
- Set operations: `intersect()` and `except_()`.
- Analyst clauses: `distinctOn()`, `groupByRollup()`, `groupByCube()`, `groupByGroupingSets()`, `qualify()`.
- `for_update(skip_locked, nowait)` row locking on Postgres.
- `total()` count helper and `cursor_page()` keyset pagination.
- `explain(analyze=False)` plan inspection.
- Through-relation (`ManyToManyRelation`) eager loading in `withGraphFetched()`.
- Per-dialect function name translation: `NOW()` renders as `GETDATE()` on MSSQL and the reverse; `LENGTH()` renders as `LEN()` on MSSQL.

## 2.0.0 (2026-08-14)

### Breaking changes

- String arguments to `select_func()` and the dynamic function methods are now column references, not string literals. Wrap literal values in `Literal()`. A string argument that is not a plain column name raises `ValueError`.
- Operators passed to `where()` and `having()` are validated against an allowlist. Unrecognized operators raise `ValueError`. Use `QueryBuilder.raw()` for raw predicates.
- `top()` raises `DialectError` on dialects other than MSSQL. It previously disappeared from the query without warning.
- On MSSQL, `limit()` and `offset()` raise `DialectError` when the query has no `ORDER BY`, because T-SQL rejects OFFSET/FETCH without one.
- `whereILike()` compiles to `LOWER(col) LIKE LOWER(pattern)` on dialects without native ILIKE. Postgres keeps native `ILIKE`.
- Booleans render as `TRUE`/`FALSE`, or `1`/`0` on MSSQL. They previously rendered as the Python words `True` and `False`.
- Duplicate CTE aliases with different definitions raise `ValueError` instead of silently keeping the last one.
- `update()` and `delete()` refuse to render without a `where()` clause.
- Empty `whereIn()` lists raise `ValueError`.
- Column references in WHERE, HAVING, and GROUP BY clauses now quote per dialect when they are plain identifier paths.
- `with_()` requires a `QueryBuilder` and renders it lazily; later changes to the CTE subquery are reflected in the output.
- The declared Python floor is now 3.9.

### Added

- `to_sql()` returns the statement as `(sql, params)` with dialect placeholders (`?` by default, `%s` for Postgres).
- `insert()`, `update()`, `delete()`, and `returning()` statement builders.
- Query execution: `Model.bind(connection)`, `run()`, and `first()` against any DB-API 2.0 connection, with rows hydrated into model instances.
- `withGraphFetched()` eager loading for HasMany, HasOne, and BelongsToOne relations.
- Class-level column access (`User.id`), an optional `columns` declaration that rejects typo'd column names, and a model registry that resolves string `modelClass` references across modules.
- `clone()` for branching from a shared base query and `page()` for zero-based pagination.
- snake_case aliases for every camelCase query method.
- Window functions accept arguments, frame clauses, and ORDER BY directions.
- Select lists accept the `'column AS alias'` shorthand.
- Comparing a column to `None` with `=` or `!=` renders `IS NULL` / `IS NOT NULL`.

### Fixed

- `copy.copy`, `copy.deepcopy`, and `pickle` no longer recurse infinitely on builders and models.
- Union members keep their own `ORDER BY` and `LIMIT` instead of dropping them.
- CTEs on FROM subqueries and on other CTEs hoist into the top-level `WITH` clause instead of rendering invalid nested `WITH` statements.
- `GROUP BY` no longer quotes a dotted path as a single identifier.
- MSSQL quotes dotted identifier paths, and Presto renders `OFFSET` before `LIMIT`.
- `limit()`, `offset()`, and `top()` reject booleans and negative numbers.
- Aliased `joinRelated()` no longer crashes when the join's `to` reference has no table prefix.
- The deploy script rolls back the version bump when the build fails and pushes the release commit along with its tag.

## 1.1.0

Initial public feature set: SELECT query building with joins, relations, CTEs, unions, window and CASE expressions, and dialect-aware compilation for the default, Postgres, MSSQL, and Presto dialects.
