# Changelog

## 2.8.0 (2026-08-14)

### Added

- Repeatable migrations: a `<id>.repeat.sql` file, or `Migration(id, up, repeatable=True)`, re-runs whenever its checksum is new or changed, for views, functions, and seed data. Repeatables run after every versioned migration on every `up()` call, including targeted ones. A re-run updates the tracking row in place and keeps its original sequence number. `down()` and `down_to()` never revert them, the out-of-order check ignores them, and `baseline()` records them at their current checksum so adoption does not re-run objects the schema already holds. Validation treats a changed repeatable checksum as the re-run signal, not a problem. A repeatable cannot have a down step, cannot share an id with a versioned migration, and needs an explicit `checksum` when its step is callable.
- `statuses()` on `Migrator` and `AsyncMigrator`: (id, state) pairs with the states `applied`, `pending`, and `changed`. The CLI `status` command prints these states. `status()` keeps its (id, applied) shape.
- Placeholders in SQL migration files: `${key}` markers fill from `load_migrations(directory, placeholders=...)` or the config module's `placeholders` dict. Passing a mapping turns substitution on; a key with no value then raises `ValueError` naming the file and the key, and `$${` escapes to a literal `${`. With no mapping, files load untouched, so existing files that happen to contain `${...}` keep working. Substitution runs before checksums compute, so changing a value after a migration applied flags a checksum mismatch.

### Changed

- `pending()` also returns repeatables whose checksum changed, since the next `up()` will run them.
- `load_migrations()` now rejects a `.sql` file only when it matches none of the three suffixes; the error message names all of them.

## 2.7.0 (2026-08-14)

### Added

- Migrations as SQL files: `load_migrations(directory)` pairs `<id>.up.sql` files with optional `<id>.down.sql` files and returns `Migration` objects ordered by id. Statements split at line-ending semicolons, so semicolons inside string literals stay intact. Empty files, orphaned down files, and misnamed `.sql` files raise `ValueError`.
- `baseline(target)` on `Migrator` and `AsyncMigrator`: records migrations up to and including the target as applied without running them, for adopting a database whose schema already matches. Rows carry real checksums and a null execution time.
- `Migrator.plan(models, ...)`: the migration `sync()` would generate, returned without registering or applying it, or `None` when the schema is current.
- A command-line runner: the `sustained` console script and `python -m sustained` drive a `Migrator` from a config module. Commands: `status`, `migrate`, `down`, `validate`, `repair`, `script`, `baseline`. Exits 0 on success, 1 on failure.

## 2.6.0 (2026-08-14)

### Added

- The migration tracking table now records a monotonic sequence number, a SHA-256 checksum of the up statements, the execution time in milliseconds, and a success flag. Apply order reads from the sequence number instead of timestamp ties. Tables written by earlier versions upgrade in place on first use; on Athena the upgrade needs an Iceberg tracking table, the same requirement `down()` already has.
- `Migrator.validate()` and `AsyncMigrator.validate()`: check the tracking table against the registered migrations and raise `MigrationError` on failed attempts, applied ids the migrator does not know, checksum mismatches from edited migrations, and pending migrations ordered before applied ones.
- `repair()`: deletes rows left by failed attempts and rewrites stored checksums that drifted, including null checksums on rows written before checksums existed.
- On engines without transactions, a failing step writes a row with the success flag off, so the interrupted run is visible and blocks the next `up()` until repaired.
- Migration runs hold an exclusive advisory lock named after the tracking table, so concurrent migrators queue instead of racing: `pg_advisory_lock` on Postgres, `sp_getapplock` on MSSQL. SQLite and DuckDB serialize writers on their own; Athena has nothing to lock with.
- `Migration` accepts an explicit `checksum` for callable steps, and `migration_checksum()` exposes the value validation compares.
- `applied_records()` returns the tracking rows with sequence, checksum, and success flag.

### Changed

- `up()` validates before running. Pass `validate=False` to skip the checks or `allow_out_of_order=True` to accept a pending migration ordered before an applied one; earlier versions applied out-of-order migrations silently.

### Fixed

- The tracking table upgrade backfill no longer overwrites values that already exist: it touches only the columns the current run added and only rows where they are still null, so a recorded failed attempt survives an interrupted earlier upgrade.

## 2.5.0 (2026-08-14)

### Added

- AWS Athena dialect (`Dialects.ATHENA`): inherits Presto's query behavior with Athena's differences. `%s` placeholders matching pyathena, MERGE upserts on Iceberg tables, and Athena's type spellings (INT, STRING, DOUBLE, DECIMAL; JSON maps to STRING).
- `TableOptions(location, partitioned_by, properties)`: storage clauses declared as a model's `tableOptions` and rendered as PARTITIONED BY, LOCATION, and TBLPROPERTIES on Athena. Other dialects raise when options are set. Partition entries pass through as written so Iceberg transforms work.
- Athena DDL: `ADD COLUMNS` spelling for added columns, `CHANGE COLUMN` for Iceberg type widenings. Constraints (primary key, unique, default, foreign key, NOT NULL, autoincrement) and indexes raise `DialectError` at build time because Athena cannot enforce them. Renames, nullability changes, `type_casts` hints, RETURNING, and temporary CTAS raise with directions.
- Migrations on engines without transactions: the migrator now consults the dialect and runs each step bare on Athena instead of wrapping it in a transaction, never calling rollback. `Migrator` and `AsyncMigrator` accept `tracking_table_options` for the tracking table's storage clauses, and create it without constraints on constraint-free engines.
- The function registry recognizes Athena wherever it recognizes Presto, including `NOW()` and the `GETDATE()` translation.
- Schema diffing normalizes Athena's STRING type, so tables created from models diff clean through `information_schema`.

## 2.4.0 (2026-08-14)

### Added

- Constraint-aware introspection: primary keys, unique constraints, foreign keys, column defaults, and indexes are read from SQLite PRAGMA tables or information_schema, with graceful degradation and system schemas filtered.
- Type and nullability changes now generate migrations: in-place reversible ALTER COLUMN on Postgres (with `type_casts` USING hints), MSSQL, and DuckDB; automatic table rebuild with row copy on SQLite.
- Rename hints: `renames={'table.old': 'new'}` and `table_renames` produce reversible RENAME statements (sp_rename on MSSQL) instead of destructive drop-plus-add.
- Declared indexes on models via `Index`; created with the table and diffed for additions, definition changes, and opt-in drops, all reversible.
- `backfill` on ColumnDef: NOT NULL adds and tightenings emit add-nullable, UPDATE, SET NOT NULL, or fold into the SQLite rebuild.
- Length and precision changes detected when both sides report them.
- Constraint notes: PK, FK, unique, and default drift reported in the diff, never auto-migrated.
- Offline scripts: `migration_sql()` and `Migrator.script()` render the SQL a run would execute for DBA review.
- `AsyncMigrator`: the migration runner on an AsyncAdapter with transactional application and awaited callable steps.

## 2.3.0 (2026-08-14)

### Added

- Schema autogeneration: `diff_schema()` introspects the live database (SQLite PRAGMA on the default dialect, `information_schema.columns` elsewhere) and reports missing tables, new columns, extra objects, and changed columns with a readable `summary()`. Type comparison round-trips through each dialect's own type mapping, so tables created from models diff clean.
- `autogenerate()`: builds a `Migration` from the diff. Additive steps are reversible (CREATE/DROP TABLE, ADD/DROP COLUMN pairs). Drops require `allow_drops=True` and carry no down step; changed column types block generation unless explicitly ignored; NOT NULL adds without defaults and primary key or autoincrement adds are rejected.
- `Migrator.sync(models)`: diff, generate, register, and apply in one call, idempotent when the schema is current. `Migrator.down_to(id)` reverts newest-first until the target is the most recent applied migration.
- Compilers render `ADD COLUMN` and `DROP COLUMN` statements, with the T-SQL `ADD` spelling on MSSQL.

## 2.2.0 (2026-08-14)

### Added

- Typed column definitions: models declare `tableColumns` with `Integer`, `BigInteger`, `String`, `Text`, `Boolean`, `Float`, `Numeric`, `Date`, `Timestamp`, and `Json`, including composite primary keys, defaults, unique constraints, foreign key references, and autoincrement. Strict column access derives automatically.
- Model-driven DDL: `create_table_sql()`, `create_table()`, `drop_table()` with per-dialect type mapping and identity syntax. DuckDB and Presto raise for autoincrement.
- Migration runner: ordered `Migration` objects with up/down steps (SQL, statement lists, or callables), a self-creating tracking table, transactional application, stop-after targets, and newest-first reverts. `create_table_migration()` derives create/drop pairs from models. No catalog diffing.
- `ConnectionPool`: thread-safe, lazy, bounded pooling for DB-API connections. `Model.bind()` and all execution entry points accept a pool; transactions pin one checked-out connection to the thread; nested blocks reuse it via savepoints.
- Async execution: `arun()`, `afirst()`, `ato_dicts()` through an adapter interface with `DbApiAsyncAdapter` (any sync driver via worker threads), `AiosqliteAdapter`, and `AsyncpgAdapter` (`%s` to `$n` conversion). `Model.bind_async()` and `async_transaction()` with ContextVar pinning. Async through-relation eager loading and nested async transactions are not supported yet.

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
