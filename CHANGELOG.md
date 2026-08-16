# Changelog

## 2.14.0 (2026-08-16)

### Added

- A passing rehearsal writes a receipt: one row in a new table, `sustained_rehearsals`, created on first use like the tracking table. The row is keyed by a SHA-256 over the checksums of the applied migrations the run started from and the checksums of the migrations it ran. A failing rehearsal records the failure under the same key.
- `Migrator.up()` and `AsyncMigrator.up()` refuse to apply a statement that removes data, meaning a DROP TABLE, a column drop, or a TRUNCATE, unless a passing receipt covers that exact set. The error names the migration and the statement. `up(unrehearsed=True)` applies them anyway, and `sustained migrate --unrehearsed` is the same door from the shell. A run that only adds is never gated and never reads the table.
- A rehearsal also records a receipt for each shorter run a `--target` would produce that removes data, since it applied and reverted those on its way through. One rehearsal covers the whole run and every target within it.
- `RehearsalRequired`, in `sustained.exceptions` and re-exported at the package root, is what the refusal raises.
- `record_rehearsal(key, outcome)`, `rehearsal_outcome(key)`, and `rehearsed(key)` on both migrators, and `receipt_key(applied, run)` in `sustained.migrations`, for recording and reading a receipt directly.
- `rehearse --json` gains `key` and `recorded`, and the plain report prints `receipt recorded` when the proof lands.
- The `Migrator` and `AsyncMigrator` constructors take `rehearsal_table`, and the CLI config module takes the same name.

### Changed

- `rehearse()` returns a `Rehearsal` rather than a plain list. It subclasses `list`, so iterating and indexing are unchanged, and it carries `key`, `recorded`, and `ok`.
- `rehearse(scratch=True)` records nothing through the API, since a receipt belongs on the database the next run will read rather than on a throwaway one. The key comes back on the result. The CLI writes it on the real database after a passing scratch run, keyed against that database's applied history and pending set, and only when the scratch run applied every migration pending there.
- `sustained plan` prints `run: sustained rehearse` instead of `run: sustained migrate` when a pending migration removes data, since migrate would refuse it.
- Both Sustained tables are excluded from every diff against the models, so the new one never reads as drift or as an object a down step left behind.
- `rehearsal_failed(result)` moved from `sustained.cli` to `sustained.migrations`, so the rule that decides whether a rehearsal passed lives with the API.

## 2.13.0 (2026-08-16)

### Added

- `Migrator.up(models=[...])` diffs the models against the database, applies the generated migration with everything else pending, and takes the diff options `plan()` takes. It replaces `sync()`, so the three verbs cover the whole job: `plan` tells the truth, `rehearse` proves it, `migrate` applies it. The diff is taken after the pending migrations have run, so it sees the schema they left. A target cannot be combined with models, since the generated migration always runs last.
- `sustained migrate` and `sustained rehearse` pass the config module's `models` when it names any, so the model diff reaches the shell for the first time. A targeted `migrate` still applies the registered migrations only.
- `Migrator.rehearse(models=[...])` rehearses the generated migration alongside the pending ones. It never registers it and the rollback still leaves the database untouched.
- A rehearsal now reports what the schema said, not only that the statements ran. `landed` says the models arrived, for the generated migration only, since a hand-written migration may create objects no model declares. `reversed` compares the schema after the down sweep against a snapshot taken before the run, so a down step that runs without taking its change back is reported. Both appear on `RehearsalResult` as optional lists: `None` was not checked, `[]` was proved, and a non-empty list names the trouble. The rehearsal report prints the words and lists the objects underneath, and exits 1 when either check fails.
- Tables and columns are compared for `reversed`. Indexes, constraints, and column defaults are not, so a leftover index after a down step is not detected yet. A database that will not report its schema leaves the check unchecked rather than failing the run.
- `sustained rehearse --json`.
- `sustained migrate` re-reads the schema after a successful run when the config module names models, and prints either that the schema matches them or the differences left. It is a report, never a gate.
- `Migrator.drift(models)` returns those differences directly: what the models still ask for, one readable line each. Objects the database holds and the models do not are left out; use `plan()` for the full comparison, drops included.
- `diff_snapshots(before, after)` in `sustained.autogenerate` compares two introspected schemas, and `async_introspect_schema(adapter, dialect)` reads a schema through an async adapter. `AsyncMigrator.rehearse()` uses both for its own `reversed` check. There is no models argument on the async side: diffing models against a database is a synchronous path.

### Changed

- `plan --json` reports each pending migration's `statements` as a list of objects carrying the SQL and whether it is destructive, rather than a count, and `drift` holds the same objects. A script reading the old count needs updating. `PendingSummary.statements` became `PendingSummary.sql`, the statement list.
- The generated diff no longer refuses to run when the database holds objects the models do not declare. Hand-written migrations create such objects, and a mixed database is not a mistake. Drops still need `allow_drops=True`, and `ignore_undeclared=False` restores the refusal.
- The tracking table gained a `generated` column, added in place to tables written by earlier versions on first use, marking the rows a model diff wrote. Nothing on disk carries a generated migration's id, so validation would otherwise report each one as unknown to the next migrator that ran.
- `sustained plan` prints `run: sustained migrate` for both pending work and drift. A drift section holding only drops says instead that migrate does not generate drops, since it does not.
- Schema introspection is written once as a query plan that the blocking cursor and the async adapter each drive. A query that fails is handed back to the plan, so the `information_schema` read still degrades to column-only data where the constraint views are missing.

### Deprecated

- `Migrator.sync()`. It raises a `DeprecationWarning` and delegates to `up(models=[...])`. It goes away in 3.0.

## 2.12.0 (2026-08-16)

### Added

- `withGraphFetched()` takes a dotted path, such as `'shows.tickets'`, and loads every level. Each relation costs one query per level, batched over every parent at that level with `WHERE fk IN (...)`, so a deeper graph never becomes a query per row. Paths that share a prefix load the prefix once. An unknown segment raises when the query is built, naming the segment, the model it was read from, and the full path. Writes through a graph path are still unsupported.
- Async eager loading covers relations that run through a link table, and dotted paths beyond their first segment. The sync loader split into a planner and an attacher that both paths call, so the SQL and the row grouping are written once.
- `async_transaction()` nests through ANSI savepoints, matching `transaction()`. An inner block that raises rolls back to its savepoint and leaves the outer block open.

### Fixed

- The type stubs now describe the join and clause methods the runtime accepts. `whereRaw`, `havingRaw`, and their `and` and `or` forms were absent, so a type checker rejected working code. `outerJoin` and `OuterJoinRelated` were declared but have never existed, and `fullJoin`, `fullOuterJoin`, and `crossJoinRelated` were missing. The raw join form is now three overloads, one per calling shape, and the relation form takes the `alias` argument it has always accepted. A test compares each stub against the runtime in both directions, since `__getattr__` resolves most of this surface and a type checker cannot see the drift.
- `LENGTH` is registered once. It was registered twice, and the second registration, which carries the T-SQL `LEN` spelling, overwrote the first.
- `IntrospectedTable` and `FunctionMetadata` default their mapping fields to read-only empty mappings. A NamedTuple shares one default object across every instance, so a mutable default lets one table's or one function's mapping become another's.

## 2.11.0 (2026-08-14)

### Fixed

- `repair()` no longer rewrites the stored checksum of a changed repeatable. For a repeatable the changed checksum is what schedules the re-run, so the rewrite cancelled the run and the new contents never reached the database. Failed-attempt rows for repeatables are still removed. Both migrators are covered.
- A malformed placeholder marker in a migration file, such as `${my-key}` or an unclosed `${key`, now raises `ValueError` naming the file instead of passing through to the database as raw SQL. This applies only when a placeholders mapping is given; with none, files still load untouched.
- `rehearse()` reads validation state, pending migrations, and applied records inside the advisory lock, so a concurrent migrator cannot apply between the read and the rehearsal.
- `AsyncMigrator.rehearse()` refuses a connection in autocommit mode, as `Migrator.rehearse()` already did.
- Tagging an exception with its migration id no longer raises on exception types that reject new attributes, which would have masked the original error.
- `sustained plan` prints `run: Migrator.sync(models)` when it finds model drift. Drift is closed by `sync()`, not by `migrate`, and drift-only output previously offered no next step.

### Changed

- A targeted `up()` no longer runs the repeatables. A repeatable may depend on a versioned migration past the target, and running it against the half-migrated schema fails. The next full `up()` runs it.
- The destructive scan also labels a column drop written without the COLUMN keyword, as MySQL allows. Drops of constraints, indexes, and keys stay unlabelled.
- The refusal message for rehearsing a non-rehearsable dialect mentions `scratch=True` for library callers alongside the config module hook.
- The docs cover the default dialect's place on the rehearsable list, since the check reads the declared dialect rather than the engine, and scratch databases that keep rehearsed objects between runs.

## 2.10.0 (2026-08-14)

### Added

- `sustained rehearse` and `Migrator.rehearse()`: applies every pending migration, runs the down steps back down, and rolls the whole thing back, so the database ends where it started. It reports whether each up step ran and whether each down step ran, exits 1 when a step failed, and exits 0 otherwise. A migration with no down step is not a failure; its line says so, and the migrations older than it report that the sweep never reached them, since they sit under changes that cannot be taken back. Repeatables run in their usual place and have no down step to prove. The tracking rows a rehearsal writes roll back with everything else, so the migrations stay pending. `AsyncMigrator.rehearse()` is the same on an adapter.
- Only databases whose schema changes roll back may rehearse: SQLite, Postgres, and DuckDB. The others raise with the reason. So does a connection in autocommit mode, and one inside an open `transaction()` block, whose work the rollback would take back too. A config module that defines `get_rehearsal_connection()` sends the rehearsal to a scratch database instead, where the dialect check does not apply and the changes may survive the rollback. A scratch database is usually empty, so the whole history replays there rather than what is pending on the real one.
- `Compiler.begin_transaction_sql()` and `Compiler.rollback_transaction_sql()`: the statements that open and take back a transaction, which a rehearsal uses instead of the driver's own calls. Drivers disagree on when a transaction exists: SQLite opens one for INSERT but not for CREATE TABLE, and asyncpg runs in autocommit until one is opened, with an adapter whose `rollback()` does nothing. Both would leave a rehearsal's changes in place. MSSQL spells them `BEGIN TRANSACTION` and `ROLLBACK TRANSACTION`; engines without transactions return `None`. The rehearsal rolls back before it begins, so the explicit statement does not land inside a transaction the reads already opened.
- Config module callbacks around `sustained migrate`: `before_migrate(connection)` before the run starts, `after_migrate(connection, applied)` when at least one migration applied, and `on_error(connection, migration_id, error)` after a failure and before it reaches the shell. A callback that raises has its own error printed on stderr, and the migration error still decides the exit code. Only `migrate` calls them; `rehearse` does not, since nothing real happened.
- `Migrator.connection` and `AsyncMigrator.adapter` properties.

### Changed

- A failing statement on the command line prints as an error line naming the migration instead of a traceback. Drivers raise their own error classes, and only `MigrationError` and `ValueError` were caught before. The same applies to a connection that will not open and a migrations directory that will not load.

## 2.9.0 (2026-08-14)

### Added

- `sustained plan`: one screen showing what a run would do before it starts. It lists the pending migrations with their statement counts, the problems `validate` would report, and the drift between the config module's `models` and the database. It exits 0 when the database is current, 2 when work is waiting, and 1 when validation found problems, which win over pending work. The drift section appears only when the config module names `models`, and it reports every difference, drops included, unlike `sync()`. Note that argparse also exits 2 on a usage error, so a script that treats 2 as "work is waiting" should check stderr for an `error:` line.
- Destructive labels: a statement that drops a table, drops a column, or truncates one is labelled in the plan. The new `sustained.analysis` module holds the scan as `destructive_statements(sql)` and `summarize(migration, state)`, which touch no database and so suit async callers too. The scan is textual, so a drop named inside a string literal is labelled as well. The label informs the operator; nothing is blocked and there is no flag to gate it.
- `--json` on `status`, `validate`, and `plan`: one indented JSON object on stdout instead of the plain lines, with the exit codes unchanged. `status` prints `{"migrations": [{"id", "state"}]}`, `validate` prints `{"ok", "problems"}`, and `plan` prints `{"pending", "problems", "drift"}`. A pending entry with a callable step has a null statement count. The `drift` key is null rather than an empty list when the config module names no models, which separates "nothing was compared" from "compared and found no gap".

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
