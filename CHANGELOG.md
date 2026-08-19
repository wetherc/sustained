# Changelog

## 2.22.0

### Added

- `Binary()` in `sustained.schema` declares a bytes column. It renders `BLOB` on the default dialect, `BYTEA` on Postgres, `VARBINARY(MAX)` on SQL Server, `VARBINARY` on Presto, and `BINARY` on Athena. Introspection folds `bytea`, the `blob` variants, and `varbinary` back to the one type, so the column never drifts against its own DDL. MySQL treats it as off-row like `TEXT` and `JSON`: no unique constraint, no literal default.
- The **Covered** column on the [support page](https://sustained.tbmh.org/support) is now a proven claim. Each cover name maps to one test module in `tests/integration`, and a contract test fails the suite when `support.json` and a server's test class disagree in either direction. The integration suite behind it grew from the migration lifecycle to five covers: `queries` (joins, eager loading, aggregates, window functions, CTEs, set operations, subqueries, hydration), `writes` (upserts, RETURNING, `INSERT ... SELECT`, CREATE TABLE AS), `transactions` (commit and rollback observed from a second connection, savepoint nesting, pooling), `migrations` (now with validation failures, guards, column type round trips, and SQL file migrations), and `async` (`arun()`, `async_transaction()`, and `AsyncMigrator` on asyncpg and aiosqlite). Where a dialect refuses a feature, the suite asserts the `DialectError` and that nothing reached the server.
- `matrix.py` gains a `<name>-latest` target per container database, for example `postgres-latest`. It runs the same tests against the newest release the vendor supports, as pinned in `support.json`, so the suite runs both ends of each claimed version range.

### Fixed

- `transaction()` spells nested-transaction savepoints per dialect. It rendered ANSI `SAVEPOINT` everywhere, which SQL Server spells `SAVE TRANSACTION` and DuckDB does not have. The compiler now provides the spellings, and nesting on DuckDB raises `DialectError` before any statement is sent.
- `transaction()` now works on the duckdb driver. That driver autocommits every statement and gives every cursor its own session, so a block never opened a real transaction: inserts committed instantly and rollback failed. A transaction now pins one cursor for all statements inside it, including a migration's statements and its tracking write, so a failed multi-statement migration on DuckDB rolls back.
- `async_transaction()` renders its transaction control through the dialect compiler the same way, and nesting on a dialect with no savepoint spelling raises `DialectError`.
- sqlite3 connections in the default legacy transaction control never begin before DDL, so a rolled-back `transaction()` block kept its schema changes. `transaction()` now sends an explicit `BEGIN` on such connections.
- A NOT NULL change with a `backfill` on DuckDB compiles to `ALTER COLUMN ... SET DATA TYPE ... USING coalesce(...)`, because DuckDB refuses `SET NOT NULL` after an `UPDATE` in the same transaction.
- Plain indexes are read back on MySQL, MariaDB, SQL Server, and DuckDB. The shared `information_schema` read saw unique constraints only, so a declared index drifted on every plan and the second `up()` failed recreating it. Indexes that back foreign keys are recognized and never demand `allow_drops`.
- Set-operation members render without parentheses on the default dialect, because SQLite rejects them outright, so `union()` and its siblings now run on the default dialect's own engine. A bare member that carries its own ORDER BY or LIMIT raises `DialectError`; every other dialect keeps the parentheses and per-member clauses.

## 2.21.0

### Added

- `Enum(*values, name=...)` in `sustained.schema` declares a column whose values come from a named, ordered list. Values are strings or one Python `enum.Enum` class with string values; hydrated values stay strings. The name is required, so diffs stay stable and two models can share one type: the same name with the same values is one type, and the same name with different values raises. Postgres and DuckDB create a named type with `CREATE TYPE ... AS ENUM`, MySQL renders an inline `ENUM(...)`, and the default dialect and MSSQL render a VARCHAR held to the list by a CHECK constraint named `ck_<table>_<column>_enum`. Presto and Athena refuse the column, since neither engine can enforce it.
- Migration generation covers enum types. A value appended to the model generates `ALTER TYPE ... ADD VALUE` on Postgres, a restated list through `MODIFY COLUMN` on MySQL, and a re-created CHECK constraint elsewhere. The Postgres migration is irreversible, because Postgres has no `DROP VALUE`; PostgreSQL 12 and later roll `ADD VALUE` back inside a transaction, which is what lets `rehearse` prove it, and the support policy now states that floor. Removing or reordering values refuses with the rebuild recipe: a new type, a `USING` cast, then the old type's drop.
- `Check(name, expression)` and `ForeignKey(name, columns, references, on_delete=, on_update=)` in `sustained.schema` declare named table constraints in the new `tableConstraints` model attribute. A `ForeignKey` takes single or composite columns, `'table.column'` targets in one table, and the five referential actions; the `references` shorthand on a column stays the plain single-column form. There is no Unique constraint object, because `Index(name, *columns, unique=True)` already covers it.
- Migration generation covers those constraints. A missing constraint generates `ADD CONSTRAINT` with the drop as its down step. Changed and undeclared constraints are gated by `allow_drops`, like extra indexes. Check expressions compare normalized for case, whitespace, and outer parentheses, and a Postgres difference that survives normalization becomes a note rather than a drop, since engines rewrite the expressions they store. SQLite routes constraint changes through its table rebuild, which now renders `tableConstraints` and carries undeclared foreign keys and `ck_`-named checks across.
- `sustained.ddl` holds typed steps for hand-written migrations: `create_table`, `drop_table`, `add_column`, `drop_column`, `rename_column`, `rename_table`, `add_foreign_key`, `drop_foreign_key`, `add_check`, `drop_constraint`, `create_index`, `drop_index`, `create_enum`, `drop_enum`, `add_enum_value`, and a raw `sql()` escape hatch. A step renders through the dialect compiler when the migration runs or `script()` prints it, so one migration serves every dialect, and guards, destructive labels, and the rehearsal gate read its rendered SQL, which a callable step never gives them. Its checksum hashes the operation and its arguments rather than the rendered SQL, so an applied migration survives a dialect change.
- A `Migration` whose up step is all reversible ddl steps derives its down step: the inverses, newest first. A step that cannot reverse, which is any drop, `add_enum_value`, or `sql()`, refuses the derivation and asks for an explicit down step or `down=None`. Both migrators expose the `compiler` property the steps render through.
- Postgres introspection has a dedicated read in place of the generic `information_schema` fallback. Foreign key targets resolve to real tables with their columns, names, and referential actions, where the fallback reported `?`. Non-unique indexes, varchar lengths, numeric precision and scale, CHECK expressions, and enum value lists are all read, so declared objects stop diffing as permanently missing or changed. SQLite recovers constraint names and Sustained-generated checks from `sqlite_master`, and MySQL parses inline enum types.

### Changed

- `DROP TYPE` and `DROP CONSTRAINT` (with `DROP CHECK` and `DROP FOREIGN KEY`) count as destructive: `plan` labels them, `no_drops()` blocks them, and the rehearsal gate covers them. A guard configuration that relied on constraint drops passing now blocks them; index and key drops still pass. A dropped constraint removes no rows, but putting it back needs the data to still satisfy it.
- `supports_constraints()` is `False` on Presto, which enforces none. Declared table constraints raise `DialectError` there and on Athena, and the tracking table renders without constraints on both.

### Changed

- The row a rehearsal writes is called a rehearsal row throughout the documentation and the code, in place of the earlier word "receipt". `rehearsal_key()` replaces `receipt_key()` in `sustained.migrations`, and the outcome constants are `REHEARSAL_PASSED`, `REHEARSAL_FAILED`, and `REHEARSAL_OVERRIDE`. The stored table, its column names, and every key a database already holds are untouched, so a rehearsal recorded by an earlier version still opens the gate.
- `sustained rehearse` prints `rehearsal row recorded`, and a scratch run that covered too little prints `rehearsal row not recorded`, where both lines said `receipt` before. A script matching that text needs updating.

### Deprecated

- `receipt_key()`, `RECEIPT_PASSED`, `RECEIPT_FAILED`, and `RECEIPT_OVERRIDE` in `sustained.migrations`. Each still imports and raises a `DeprecationWarning` naming its replacement, and goes away in 3.0.

## 2.19.0

### Added

- A written support policy at [Support Policy](https://sustained.tbmh.org/support). A database is at one of two levels: `runs` means the integration suite applies migrations to a real server and reads the schema back, and `builds` means the SQL compiles and unit tests check its text. Nothing sits between the two. The page also states the Python floor, one server version per database, the three-step deprecation path, what each part of a version number promises, and where to report a security problem.
- `support.json` holds that list once. `sync_support.py` renders the tables on the support page from it, and a pre-commit hook fails when the page and the file disagree. The script refuses a `runs` row that has no `tests/integration` module, and a container row whose service is not in the compose file, so the table cannot claim coverage that does not exist.
- An integration suite in `tests/integration/`. One shared body runs against every server: apply the models, read the schema back, apply only the difference, roll it down, run a registered migration and revert it, rehearse, validate, repair, hold the advisory lock while two migrators run at once, check that a JSON column does not drift against its own DDL, and round trip a query through the driver. A server that is not there is skipped, unless `SUSTAINED_TEST_STRICT=1` turns those skips into failures.
- `matrix.py` runs that suite. It starts the servers from `docker/compose.yaml`, waits for each to report healthy, runs its module, prints one line per server, and removes the containers afterwards. A bare run takes every server the machine can serve, naming targets runs a subset, `python` runs the unit suite on each interpreter on PATH, and `--check` reports what would run without starting anything. Exit codes are 0 for a clean run, 1 for a failure, and 2 when nothing failed and something was still waiting. Setting a server's connection variable uses that server and starts no container. Athena runs in your own AWS account, needs a staging S3 directory, and is covered for queries only.
- `Compiler.compile_create_table()` renders the whole CREATE TABLE statement, so a dialect that spells the if-missing check differently can override one method.

### Fixed

- Tracking table columns now quote through the dialect compiler. `generated` is a reserved word in MySQL, so the unquoted SQL was a syntax error there: the column probe read the column as missing, and every run tried to add it again. Found by running the migration lifecycle against a real MySQL server.
- The MySQL advisory lock waits with a one-year timeout rather than a negative one. MariaDB returns NULL for a negative `GET_LOCK` timeout, so the lock was silently absent and two migrators could collide on the same statement.
- SQL Server creates the tracking tables behind an `IF OBJECT_ID(...) IS NULL` check. T-SQL has no `CREATE TABLE IF NOT EXISTS`, so every migration run against SQL Server failed on its first statement.

## 2.18.0

### Added

- `Dialects.MYSQL` compiles for MySQL and MariaDB. Identifiers quote with backticks, placeholders are `%s` to match PyMySQL and mysqlclient, upserts render `ON DUPLICATE KEY UPDATE`, `autoincrement` renders `AUTO_INCREMENT`, column changes go through `MODIFY COLUMN`, and migration runs hold a `GET_LOCK` advisory lock. `for_update()` works, with `SKIP LOCKED` and `NOWAIT` on MySQL 8.0.
- Column types render in the spelling `information_schema` reports back, so a column never drifts against the DDL that created it: `INT`, `TINYINT(1)` for `Boolean`, `DOUBLE` for `Float`, `DECIMAL` for `Numeric`, and `DATETIME` for `Timestamp`. `DATETIME` rather than `TIMESTAMP`, whose four bytes stop in 2038 and which converts time zones on the way in and out.
- MySQL introspection reads `column_type` rather than `data_type`, so a column arrives as `varchar(120)` and compares against the compiler's own spelling. It scopes every query to `DATABASE()`, since a MySQL schema is a database, and matches schemas as well as names in the constraint join, since a MySQL constraint name is only unique within its schema.
- MariaDB stores a `Json()` column as `longtext` with a `json_valid` CHECK constraint and reports the storage type. The read looks those constraints up and restores the JSON type, so the column does not report as drift no migration can close. MariaDB before 10.2.22 has no `check_constraints` view, and there the column does report as drift.
- `Compiler.supports_transactional_ddl()` reports whether a schema change taken back by a rollback really goes away. It defaults to `supports_transactions()`, so no other dialect changes. MySQL is the first engine where the two answers differ: its transactions work for rows, but every DDL statement commits as it runs.
- `Compiler.inline_references()` reports whether a `REFERENCES` clause beside a column definition creates a foreign key, with `compile_add_foreign_key()` and `compile_drop_foreign_key()` for the dialects that say no.

### Changed

- Schema reading moved from `sustained.autogenerate` to `sustained.introspect`: the `Introspected*` records, the type and default normalization, the schema plans, `introspect_schema`, `async_introspect_schema`, and `diff_snapshots`. Every name is re-exported from `sustained.autogenerate`, so existing imports keep working. The type-parameter helper is now public as `type_params`.
- Default normalization drops an empty argument list, so MariaDB's `current_timestamp()` and MySQL's `CURRENT_TIMESTAMP` compare equal. Type normalization gained `TINYTEXT`, `MEDIUMTEXT`, and `LONGTEXT`. `TINYINT` is deliberately absent: `TINYINT(1)` is how MySQL spells a boolean, and folding plain `TINYINT` into `INTEGER` would make a boolean and an integer the same column to a diff.

### Refused

- `rehearse()` refuses MySQL against the real database, because its rollback would take nothing back and the run would report a database unchanged that had changed. Pass `scratch=True` on a throwaway connection, or define `get_rehearsal_connection()` for the CLI. A migration that fails halfway leaves the statements before it applied and records a failure row, so recovery is `repair()`.
- `returning()` raises on MySQL, including against MariaDB, which supports it. One builder emitting SQL that only one of the two servers accepts is worse than neither. Use a second query, or `LAST_INSERT_ID()` through raw SQL.
- `STRING_AGG` raises rather than translating to `GROUP_CONCAT`, whose separator is a keyword and not a second argument.
- A whole `Text()` or `Json()` column takes neither a unique key, which MySQL wants a prefix length for, nor a literal `DEFAULT`, which it refuses.
- An unsigned integer column has no `tableColumns` declaration that produces it, so one already in the database reports as drift that no migration closes.

## 2.17.0

### Added

- The tracking table has a `steps` column holding the up and down statements of a migration generated from the models, as JSON. `down()` reads it, so a process that never ran the diff can still revert what the diff applied. Registered migrations store nothing there. Tables written by earlier versions add the column on first use.
- `up(unrehearsed=True)` records what it waived: a rehearsal row under the run's key with the outcome `override`. The row never opens the gate for a later run. `record_rehearsal()` accepts `'override'` alongside `'passed'` and `'failed'`.
- `migrate` exits 4 when a run that removes data has no passing rehearsal, which a pipeline can tell apart from a failure. The message carries `--target` through when the run had one.
- `Migrator.drift()` and `SchemaDiff.outstanding()` take `ignore_changed_columns`, matching the option that generated the migration.
- A block or a missing rehearsal row on the migration generated from the models names the registered migrations that already applied, on the exception's `applied` attribute. The CLI prints those ids before the error.

### Fixed

- A SQLite table rebuild dropped columns the models do not declare, along with their data and indexes. Undeclared columns now cross the rebuild with their type, nullability, uniqueness, and default, and hand-made indexes are recreated. `allow_drops=True` still drops them. An index on an expression cannot be introspected, so a rebuild loses it.
- An index on an expression crashed introspection, because SQLite reports a null column name for one. Those indexes are left out of the schema instead.
- `up(models=[...])` disabled the out-of-order validation check for hand-written migrations. The check runs again.
- A rehearsal with models and rename hints raised while checking whether the models landed: the renames had already run, so the second diff asked to rename objects that were gone. The check now runs without the hints, and honours `ignore_changed_columns`.
- A rehearsal reported "not reversed" and recorded a failed rehearsal row when any migration in the run had no down step, blaming its leftovers on the steps that did reverse. The comparison now runs only when every versioned migration in the run reversed.
- A rehearsal applied the generated migration after the repeatables, while `migrate` applies it before them.
- A scratch rehearsal recorded no rows for the shorter target sets, so `migrate --target` was refused for statements the scratch run had proved.
- The plan footer said `run: sustained rehearse` even after a rehearsal had recorded its row, and computed guard verdicts over the drops `migrate` never generates.
- `no_lock_without_timeout()` fired on every dialect, and its pattern matched any statement holding the words, such as an update of a column named `lock_timeout`. It is now Postgres only and anchored to a `SET` statement.
- Filter and write values accept `datetime`, `date`, `Decimal`, and `bytes`. Comparing a timestamp column against a datetime was an error under a strict checker.
- A sync savepoint that failed to open left the nesting depth one too high, so the next nested block reused a savepoint name.

## 2.16.1

### Added

- `Connection` and `Cursor` in `sustained.types`, re-exported from `sustained`. They are protocols listing the DB-API 2.0 methods Sustained calls, so a `sqlite3`, `psycopg`, or `pyodbc` connection matches by having those methods. Annotating a config module's `get_connection()` with `Connection` now checks.
- `Binding`, the `Union[Connection, ConnectionPool]` that `Model.bind()` and every `connection=` argument take.
- `SqlValue` and `RowValue`, splitting database values by direction. `SqlValue` is a value going in and is an alias for `object`, so a value passed where a column name belongs is an error. `RowValue` is a value read back and stays `Any`, because the driver decides the Python type.
- `ColumnDescription` and `RelationTree` in `sustained.types`, and `JsonValue` in `sustained.cli`.
- `AsyncpgConnection`, `AsyncpgRecord`, `AiosqliteConnection`, and `AiosqliteCursor` protocols in `sustained.aio`, describing what each shipped adapter calls on its driver.

### Changed

- Roughly 180 `Any` annotations across the package were replaced with the types above, with model, schema, and introspection types where those apply: `Model.tableColumns` is a `Dict[str, ColumnDef]`, `Model.indexes` a `List[Index]`, `Model.tableOptions` a `TableOptions`, the compilers take a `ColumnDef`, and the CLI takes a `ModuleType` for its config module. The `Any` annotations that remain each carry a comment giving the reason.
- Migration callbacks and callable migration steps are typed `Callable[[CallbackTarget], CallbackResult]`, where `CallbackTarget` is the connection for `Migrator` and the adapter for `AsyncMigrator`. A callable step may now return an awaitable on the async path, which `AsyncMigrator` already awaited.
- `ColumnExpr.in_()` and `not_in()` accept any sequence of values, not only a `list`. A tuple used to fall through to the subquery branch and fail.
- `ConnectionPool` closes connections by calling `close()` rather than probing for the attribute first. The DB-API requires the method.
- The builder stubs declare `render()`, `has_clauses()`, the `compiler` argument, and the method maps that `QueryBuilder` reaches for, so a checker pointed at `builder.py` sees the same surface the runtime has.

## 2.16.0

### Added

- The query builder is generic over its model. `Show.query()` is a `QueryBuilder[Show]`, and the model rides along through every clause, so `run()` is a `List[Show]`, `first()` is an `Optional[Show]`, and `arun()` and `afirst()` match. No cast and no annotation needed.
- `WriteBuilder[Model]`, returned by `insert()`, `insert_from()`, `create_table_as()`, `update()`, and `delete()`. Its `run()` is the affected row count or the RETURNING rows as dicts, which is what a write has always returned. At run time it is the same class as `QueryBuilder`, so `isinstance()` cannot tell the two apart; the split is for the type checker.
- `WriteResult` in `sustained.types`, the `Union[int, List[Dict[str, Any]]]` a write returns.
- `QueryBuilder[Show]` works in a run-time annotation: the class accepts the subscript and returns itself.

### Changed

- Argument positions that take any query are declared `QueryBuilder[Any]`, since the builder is invariant in its model. This includes `from_()`, `with_()`, `insert_from()`, the join `col2` argument, and `QueryResolvable`.
- The generic types live in `builder.pyi` only. Nothing about the running code changed, so an untyped codebase sees no difference.

### Not covered

- The select list does not narrow the result. `select('id')` still types as the whole model, and `to_dicts()` values stay `Any`. Reading a row's shape back out of the SQL is not something Python's type system can do.

## 2.15.0

### Added

- Guards: rules over the statements a run would apply. A guard reads the statement list and the dialect and returns a `Verdict(rule, verdict, statement)` for each statement it objects to, where the verdict is `block` or `warn`. `Migrator(..., guards=[...])` and `AsyncMigrator(..., guards=[...])` take them, and the CLI config module names them as `guards = [...]`.
- `sustained.guards` ships five rules, all factories: `no_drops()`, `index_must_be_concurrent()` (Postgres only, silent elsewhere), `no_table_rewrite()`, `no_lock_without_timeout()`, and `max_statements(n)`. `no_table_rewrite()` warns where the others block, since whether a change rewrites a table depends on the engine, its version, and whether the types coerce. `run_guards()`, `blocking()`, and `warnings_only()` are there for code that runs rules itself.
- `up()` raises `GuardBlocked` on a blocking verdict, before any statement runs, and prints warning verdicts on stderr. `GuardBlocked` is in `sustained.exceptions` and re-exported at the package root; `verdicts` holds what blocked the run. There is no flag that waives a guard: fix the statement, or take the rule out of the list.
- `sustained plan` runs the guards over the pending migrations and the drift statements together and prints a `guards` section beside the others, one line per verdict. In `--json`, a verdict rides on the statement object it flags, as `{"rule", "verdict"}`, and an unflagged statement carries an empty list.
- Exit code 3 means a guard blocked a statement, from `plan` and from `migrate`. Precedence is 1 (problems) over 3 (blocked) over 2 (pending work), since a plan that cannot be trusted outranks a statement that will be refused.
- `Migrator(..., callbacks=Callbacks(...))` and the same on `AsyncMigrator`, closing a parity gap: `before_migrate`, `after_migrate`, and `on_error` were reachable only through the CLI config module. `up()` calls them, the async migrator awaits a callback that returns an awaitable, and the config module keeps working the way it did.
- `dialect` property on both migrators, and `run_statements()` and `check_guards()` in `sustained.migrations` for code that reads or checks a run itself.

### Changed

- `sustained migrate` no longer calls the config module's callbacks itself: it collects them into a `Callbacks` object and the migrator calls them. The one visible difference is that `after_migrate` now fires before the post-run drift report rather than after it.
- `plan --json` statement objects gained a `guards` key, present and empty when no rule flagged the statement.
- The guards run twice on a run that includes the diff against the models, since the generated statements are not known until the registered migrations have run. The second pass reads the whole run, so a rule about the run as a whole counts all of it, and a warning already printed is not printed again.
- `rehearse` does not enforce guards. It runs against a database it is about to roll back, and blocking there would stop an operator from testing the statement they are fixing.

## 2.14.0

### Added

- A passing rehearsal writes one row in a new table, `sustained_rehearsals`, created on first use like the tracking table. The row is keyed by a SHA-256 over the checksums of the applied migrations the run started from and the checksums of the migrations it ran. A failing rehearsal records the failure under the same key.
- `Migrator.up()` and `AsyncMigrator.up()` refuse to apply a statement that removes data, meaning a DROP TABLE, a column drop, or a TRUNCATE, unless a passing rehearsal row covers that exact set. The error names the migration and the statement. `up(unrehearsed=True)` applies them anyway, and `sustained migrate --unrehearsed` is the same door from the shell. A run that only adds is never gated and never reads the table.
- A rehearsal also records a row for each shorter run a `--target` would produce that removes data, since it applied and reverted those on its way through. One rehearsal covers the whole run and every target within it.
- `RehearsalRequired`, in `sustained.exceptions` and re-exported at the package root, is what the refusal raises.
- `record_rehearsal(key, outcome)`, `rehearsal_outcome(key)`, and `rehearsed(key)` on both migrators, and `rehearsal_key(applied, run)` in `sustained.migrations`, for recording and reading a rehearsal row directly.
- `rehearse --json` gains `key` and `recorded`, and the plain report prints `rehearsal row recorded` when the proof lands.
- The `Migrator` and `AsyncMigrator` constructors take `rehearsal_table`, and the CLI config module takes the same name.

### Changed

- `rehearse()` returns a `Rehearsal` rather than a plain list. It subclasses `list`, so iterating and indexing are unchanged, and it carries `key`, `recorded`, and `ok`.
- `rehearse(scratch=True)` records nothing through the API, since the row belongs on the database the next run will read rather than on a throwaway one. The key comes back on the result. The CLI writes it on the real database after a passing scratch run, keyed against that database's applied history and pending set, and only when the scratch run applied every migration pending there.
- `sustained plan` prints `run: sustained rehearse` instead of `run: sustained migrate` when a pending migration removes data, since migrate would refuse it.
- Both Sustained tables are excluded from every diff against the models, so the new one never reads as drift or as an object a down step left behind.
- `rehearsal_failed(result)` moved from `sustained.cli` to `sustained.migrations`, so the rule that decides whether a rehearsal passed lives with the API.

## 2.13.0

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

## 2.12.0

### Added

- `withGraphFetched()` takes a dotted path, such as `'shows.tickets'`, and loads every level. Each relation costs one query per level, batched over every parent at that level with `WHERE fk IN (...)`, so a deeper graph never becomes a query per row. Paths that share a prefix load the prefix once. An unknown segment raises when the query is built, naming the segment, the model it was read from, and the full path. Writes through a graph path are still unsupported.
- Async eager loading covers relations that run through a link table, and dotted paths beyond their first segment. The sync loader split into a planner and an attacher that both paths call, so the SQL and the row grouping are written once.
- `async_transaction()` nests through ANSI savepoints, matching `transaction()`. An inner block that raises rolls back to its savepoint and leaves the outer block open.

### Fixed

- The type stubs now describe the join and clause methods the runtime accepts. `whereRaw`, `havingRaw`, and their `and` and `or` forms were absent, so a type checker rejected working code. `outerJoin` and `OuterJoinRelated` were declared but have never existed, and `fullJoin`, `fullOuterJoin`, and `crossJoinRelated` were missing. The raw join form is now three overloads, one per calling shape, and the relation form takes the `alias` argument it has always accepted. A test compares each stub against the runtime in both directions, since `__getattr__` resolves most of this surface and a type checker cannot see the drift.
- `LENGTH` is registered once. It was registered twice, and the second registration, which carries the T-SQL `LEN` spelling, overwrote the first.
- `IntrospectedTable` and `FunctionMetadata` default their mapping fields to read-only empty mappings. A NamedTuple shares one default object across every instance, so a mutable default lets one table's or one function's mapping become another's.

## 2.11.0

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

## 2.10.0

### Added

- `sustained rehearse` and `Migrator.rehearse()`: applies every pending migration, runs the down steps back down, and rolls the whole thing back, so the database ends where it started. It reports whether each up step ran and whether each down step ran, exits 1 when a step failed, and exits 0 otherwise. A migration with no down step is not a failure; its line says so, and the migrations older than it report that the sweep never reached them, since they sit under changes that cannot be taken back. Repeatables run in their usual place and have no down step to prove. The tracking rows a rehearsal writes roll back with everything else, so the migrations stay pending. `AsyncMigrator.rehearse()` is the same on an adapter.
- Only databases whose schema changes roll back may rehearse: SQLite, Postgres, and DuckDB. The others raise with the reason. So does a connection in autocommit mode, and one inside an open `transaction()` block, whose work the rollback would take back too. A config module that defines `get_rehearsal_connection()` sends the rehearsal to a scratch database instead, where the dialect check does not apply and the changes may survive the rollback. A scratch database is usually empty, so the whole history replays there rather than what is pending on the real one.
- `Compiler.begin_transaction_sql()` and `Compiler.rollback_transaction_sql()`: the statements that open and take back a transaction, which a rehearsal uses instead of the driver's own calls. Drivers disagree on when a transaction exists: SQLite opens one for INSERT but not for CREATE TABLE, and asyncpg runs in autocommit until one is opened, with an adapter whose `rollback()` does nothing. Both would leave a rehearsal's changes in place. MSSQL spells them `BEGIN TRANSACTION` and `ROLLBACK TRANSACTION`; engines without transactions return `None`. The rehearsal rolls back before it begins, so the explicit statement does not land inside a transaction the reads already opened.
- Config module callbacks around `sustained migrate`: `before_migrate(connection)` before the run starts, `after_migrate(connection, applied)` when at least one migration applied, and `on_error(connection, migration_id, error)` after a failure and before it reaches the shell. A callback that raises has its own error printed on stderr, and the migration error still decides the exit code. Only `migrate` calls them; `rehearse` does not, since nothing real happened.
- `Migrator.connection` and `AsyncMigrator.adapter` properties.

### Changed

- A failing statement on the command line prints as an error line naming the migration instead of a traceback. Drivers raise their own error classes, and only `MigrationError` and `ValueError` were caught before. The same applies to a connection that will not open and a migrations directory that will not load.

## 2.9.0

### Added

- `sustained plan`: one screen showing what a run would do before it starts. It lists the pending migrations with their statement counts, the problems `validate` would report, and the drift between the config module's `models` and the database. It exits 0 when the database is current, 2 when work is waiting, and 1 when validation found problems, which win over pending work. The drift section appears only when the config module names `models`, and it reports every difference, drops included, unlike `sync()`. Note that argparse also exits 2 on a usage error, so a script that treats 2 as "work is waiting" should check stderr for an `error:` line.
- Destructive labels: a statement that drops a table, drops a column, or truncates one is labelled in the plan. The new `sustained.analysis` module holds the scan as `destructive_statements(sql)` and `summarize(migration, state)`, which touch no database and so suit async callers too. The scan is textual, so a drop named inside a string literal is labelled as well. The label informs the operator; nothing is blocked and there is no flag to gate it.
- `--json` on `status`, `validate`, and `plan`: one indented JSON object on stdout instead of the plain lines, with the exit codes unchanged. `status` prints `{"migrations": [{"id", "state"}]}`, `validate` prints `{"ok", "problems"}`, and `plan` prints `{"pending", "problems", "drift"}`. A pending entry with a callable step has a null statement count. The `drift` key is null rather than an empty list when the config module names no models, which separates "nothing was compared" from "compared and found no gap".

## 2.8.0

### Added

- Repeatable migrations: a `<id>.repeat.sql` file, or `Migration(id, up, repeatable=True)`, re-runs whenever its checksum is new or changed, for views, functions, and seed data. Repeatables run after every versioned migration on every `up()` call, including targeted ones. A re-run updates the tracking row in place and keeps its original sequence number. `down()` and `down_to()` never revert them, the out-of-order check ignores them, and `baseline()` records them at their current checksum so adoption does not re-run objects the schema already holds. Validation treats a changed repeatable checksum as the re-run signal, not a problem. A repeatable cannot have a down step, cannot share an id with a versioned migration, and needs an explicit `checksum` when its step is callable.
- `statuses()` on `Migrator` and `AsyncMigrator`: (id, state) pairs with the states `applied`, `pending`, and `changed`. The CLI `status` command prints these states. `status()` keeps its (id, applied) shape.
- Placeholders in SQL migration files: `${key}` markers fill from `load_migrations(directory, placeholders=...)` or the config module's `placeholders` dict. Passing a mapping turns substitution on; a key with no value then raises `ValueError` naming the file and the key, and `$${` escapes to a literal `${`. With no mapping, files load untouched, so existing files that happen to contain `${...}` keep working. Substitution runs before checksums compute, so changing a value after a migration applied flags a checksum mismatch.

### Changed

- `pending()` also returns repeatables whose checksum changed, since the next `up()` will run them.
- `load_migrations()` now rejects a `.sql` file only when it matches none of the three suffixes; the error message names all of them.

## 2.7.0

### Added

- Migrations as SQL files: `load_migrations(directory)` pairs `<id>.up.sql` files with optional `<id>.down.sql` files and returns `Migration` objects ordered by id. Statements split at line-ending semicolons, so semicolons inside string literals stay intact. Empty files, orphaned down files, and misnamed `.sql` files raise `ValueError`.
- `baseline(target)` on `Migrator` and `AsyncMigrator`: records migrations up to and including the target as applied without running them, for adopting a database whose schema already matches. Rows carry real checksums and a null execution time.
- `Migrator.plan(models, ...)`: the migration `sync()` would generate, returned without registering or applying it, or `None` when the schema is current.
- A command-line runner: the `sustained` console script and `python -m sustained` drive a `Migrator` from a config module. Commands: `status`, `migrate`, `down`, `validate`, `repair`, `script`, `baseline`. Exits 0 on success, 1 on failure.

## 2.6.0

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

## 2.5.0

### Added

- AWS Athena dialect (`Dialects.ATHENA`): inherits Presto's query behavior with Athena's differences. `%s` placeholders matching pyathena, MERGE upserts on Iceberg tables, and Athena's type spellings (INT, STRING, DOUBLE, DECIMAL; JSON maps to STRING).
- `TableOptions(location, partitioned_by, properties)`: storage clauses declared as a model's `tableOptions` and rendered as PARTITIONED BY, LOCATION, and TBLPROPERTIES on Athena. Other dialects raise when options are set. Partition entries pass through as written so Iceberg transforms work.
- Athena DDL: `ADD COLUMNS` spelling for added columns, `CHANGE COLUMN` for Iceberg type widenings. Constraints (primary key, unique, default, foreign key, NOT NULL, autoincrement) and indexes raise `DialectError` at build time because Athena cannot enforce them. Renames, nullability changes, `type_casts` hints, RETURNING, and temporary CTAS raise with directions.
- Migrations on engines without transactions: the migrator now consults the dialect and runs each step bare on Athena instead of wrapping it in a transaction, never calling rollback. `Migrator` and `AsyncMigrator` accept `tracking_table_options` for the tracking table's storage clauses, and create it without constraints on constraint-free engines.
- The function registry recognizes Athena wherever it recognizes Presto, including `NOW()` and the `GETDATE()` translation.
- Schema diffing normalizes Athena's STRING type, so tables created from models diff clean through `information_schema`.

## 2.4.0

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

## 2.3.0

### Added

- Schema autogeneration: `diff_schema()` introspects the live database (SQLite PRAGMA on the default dialect, `information_schema.columns` elsewhere) and reports missing tables, new columns, extra objects, and changed columns with a readable `summary()`. Type comparison round-trips through each dialect's own type mapping, so tables created from models diff clean.
- `autogenerate()`: builds a `Migration` from the diff. Additive steps are reversible (CREATE/DROP TABLE, ADD/DROP COLUMN pairs). Drops require `allow_drops=True` and carry no down step; changed column types block generation unless explicitly ignored; NOT NULL adds without defaults and primary key or autoincrement adds are rejected.
- `Migrator.sync(models)`: diff, generate, register, and apply in one call, idempotent when the schema is current. `Migrator.down_to(id)` reverts newest-first until the target is the most recent applied migration.
- Compilers render `ADD COLUMN` and `DROP COLUMN` statements, with the T-SQL `ADD` spelling on MSSQL.

## 2.2.0

### Added

- Typed column definitions: models declare `tableColumns` with `Integer`, `BigInteger`, `String`, `Text`, `Boolean`, `Float`, `Numeric`, `Date`, `Timestamp`, and `Json`, including composite primary keys, defaults, unique constraints, foreign key references, and autoincrement. Strict column access derives automatically.
- Model-driven DDL: `create_table_sql()`, `create_table()`, `drop_table()` with per-dialect type mapping and identity syntax. DuckDB and Presto raise for autoincrement.
- Migration runner: ordered `Migration` objects with up/down steps (SQL, statement lists, or callables), a self-creating tracking table, transactional application, stop-after targets, and newest-first reverts. `create_table_migration()` derives create/drop pairs from models. No catalog diffing.
- `ConnectionPool`: thread-safe, lazy, bounded pooling for DB-API connections. `Model.bind()` and all execution entry points accept a pool; transactions pin one checked-out connection to the thread; nested blocks reuse it via savepoints.
- Async execution: `arun()`, `afirst()`, `ato_dicts()` through an adapter interface with `DbApiAsyncAdapter` (any sync driver via worker threads), `AiosqliteAdapter`, and `AsyncpgAdapter` (`%s` to `$n` conversion). `Model.bind_async()` and `async_transaction()` with ContextVar pinning. Async through-relation eager loading and nested async transactions are not supported yet.

## 2.1.0

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

## 2.0.0

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

### Added

- Dialect-specific query compilation. A query builds once and compiles for a chosen dialect, starting with the default, PostgreSQL, MSSQL, and Presto compilers.
- A function registry with per-dialect validation. `select_func()` and the fluent function methods check the function against the target dialect and raise `DialectError` at build time when the dialect does not support it.

### Changed

- Function rendering moved into the compiler, so every dialect renders function calls through one path.

## 1.0.2

### Fixed

- The type stubs declare the join methods they were missing.

## 1.0.1

### Fixed

- The package include path for the type stubs, so installs get them.

## 1.0.0

### Added

- Type stub files for the builders, packaged with the distribution.
- The deploy script tags each release.

## 0.0.7

### Added

- `USING` clauses on joins, and subqueries in JOIN ON clauses.
- LIKE and NULL checks in WHERE and HAVING clauses.

## 0.0.6

### Added

- `distinct()` on the query builder.
- `avg()`, `min()`, and `max()` aggregate methods.
- `Func`, for calling any SQL function, and `Subquery`, for embedding a subquery in the SELECT list.

## 0.0.5

### Added

- A select clause builder with fluent methods for complex select lists.
- `Column`, for marking a value as a column reference rather than a literal.
- The expression classes export from the top-level package.

### Changed

- `Any` annotations across the codebase were replaced with specific types.

## 0.0.4

### Added

- ORDER BY, LIMIT, TOP, and OFFSET clauses.
- UNION queries.
- Subqueries in FROM expressions and in conditional clauses, and EXISTS and BETWEEN conditions.

## 0.0.3

First tagged release. SELECT query building with joins, WHERE, GROUP BY, and HAVING clauses, relation-aware joins through `joinRelated()`, and a builder split into per-clause components.

### Fixed

- `andWhere()` could start a WHERE clause on its own.
