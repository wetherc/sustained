# Changelog

## 2.24.1

### Fixed

- A sqlite3 connection opened with `connect(factory=...)` was not recognized, so a non-transactional migration kept the implicit transaction and SQLite ignored the foreign key pragmas a rebuild needs. Detection now tests the class, not the module name.
- A schema read on Presto or Trino no longer raises when two schemas contain a table with the same name. Those catalogs read every schema they can see, and the API offers no way to narrow the read, so the refusal left those callers stuck. They keep the constraint join fallback instead. Dialects whose read is scoped to a schema, such as Postgres, still refuse the duplicate name.
- A statement handed the pool inside `async_transaction(pool)` runs on the adapter the block checked out. It checked a second adapter out, so the write ran outside the transaction and committed on its own, and a pool of one adapter deadlocked into `PoolTimeout`. A nested `async_transaction(pool)` had the same defect and now opens a savepoint on that adapter, the way the blocking `transaction(pool)` nests.
- `ConnectionPool.release()` attempts the rollback on every release. It stopped asking after one driver refusal, which duckdb produces whenever no transaction is open, so a connection released later with a transaction open went back to the idle queue still inside it and the next caller inherited its locks and its snapshot.
- `AsyncConnectionPool.release()` probes an adapter whose rollback raised with `SELECT 1` and keeps the one that answers. It dropped the adapter for any rollback error, and duckdb raises whenever no transaction is open, so every release closed and reopened the connection and an in-memory duckdb database lost its contents after the first one.
- A rehearsal leaves a migration with `transactional=False` out of the run. It ran the migration inside the rehearsal transaction, where `CREATE INDEX CONCURRENTLY` raises and SQLite ignores the rebuild pragmas, so the rehearsal failed a migration a real `up()` applies and a destructive run that contains one could never earn its rehearsal row. The result reports `up_ok` as `None` with the reason, and the run can still pass.
- The model registry keeps the newest class when a module reload rebuilds one. The identity comparison read the rebuilt class as a second definition, so a dev-server autoreload or a re-run notebook cell marked the name ambiguous forever and every string reference to it raised, naming the same class twice.
- `AsyncMigrator`'s schema read runs through the shared guarded loop, so on Postgres each catalog query takes a savepoint and one missing view no longer poisons the transaction for every statement after it. `async_introspect_schema()` gains a `recorder` argument that receives each plan statement and its rows, which is how the migrator keeps its recording; the savepoints stay out of it.
- A type or nullability change on MySQL and SQL Server restates the column's current default and comment, read from the catalog, instead of the model's. The restated definition folded a default or comment drift in silently, and the down step wrote the model's default over the one the column had, so the round trip lost it. Such a drift stays a note on the diff.
- The rehearsal prefix keys compute each migration's checksum once. The slice loops recomputed it per start and end pair, which on a run of fifty pending migrations hashed the statements over a thousand times.

## 2.24.0

### Added

- A migration can run outside a transaction: `Migration(transactional=False)`, or a `-- sustained: no transaction` comment in a SQL file. `CREATE INDEX CONCURRENTLY` needs this. A failure leaves the earlier statements applied; clean up and run `repair()`.
- `AsyncMigrator` runs the whole workflow. It gains `script()`, `plan()`, and `drift()`, and `up()` and `rehearse()` take `models` and the diff options. The async path records its schema read with `SchemaRead` and replays the recording into the diff.
- `AsyncConnectionPool` opens adapters from an async factory up to `max_size` and works with `Model.bind_async()`. It refuses `fetch()`, `execute()`, and `commit()` on itself; use `pool.scope()`, which pins one connection for a statement and its commit.
- Guards see migration boundaries: each statement arrives as `MigrationStatement`, a `str` subclass carrying the migration id and its transaction flag. `no_lock_without_timeout` now scopes a `SET LOCAL lock_timeout` to its own migration's transaction.
- Check constraints are read on MySQL, MariaDB, SQL Server, and DuckDB, so a declared `Check` diffs there. Presto and Athena stay unread through the new `reads_checks` flag. Engine-written NOT NULL and `json_valid` checks are filtered out.
- DuckDB enum types are read from `duckdb_types()`, so a type no column uses stops reading as absent and generation no longer emits a duplicate `CREATE TYPE`. A DuckDB too old for the view falls back to the old inference.
- `crossJoin()` accepts the table on its own, since a cross join has no condition.

### Fixed

- A `Subquery` in a select list, function argument, or join condition renders with placeholders through the statement's parameters. It used to inline its values as literals, which defeats statement caching. `str()` on a builder still inlines.
- A nested `CASE`, `Func`, aggregate, window, or `col()` used as a function argument or comparison value renders through the active compiler, via the new `Compiler.format_operand()`. It used to render for the wrong dialect or bind the object as a parameter.
- `CaseExpression.__str__` escapes a string result through `compile_case`, so a result such as `O'Brien` no longer breaks the SQL.
- Identifiers double the quote character inside a name on every dialect, so a name carrying the delimiter cannot end the quoted span early.
- `on()`, `andOn()`, and `orOn()` validate the join operator against the set `where()` accepts, so outside input cannot append SQL to the ON clause.
- Raw SQL fragments count `?` markers with a quote-aware scan; a question mark inside a string literal no longer miscounts or shifts values.
- A multi-row insert whose values include a raw `Expression` runs as one statement rather than through `executemany()`. Batch inserts also report their row values to the statement listener.
- An offset with no limit renders `LIMIT -1 OFFSET n` on the default dialect, which SQLite accepts. Postgres and DuckDB keep their bare `OFFSET` through the new `compile_offset_without_limit` hook.
- Eager loading gives each parent its own list of children; two parents that share a join key no longer share one list object.
- A query handed an explicit pool inside `transaction(pool)` runs on that block's pinned connection instead of checking out a second one and committing on its own.
- Every cursor closes when its statement finishes, through `execution.cursor_scope()`, which stops pyodbc and MySQL "commands out of sync" errors. `close()` joins the `Cursor` protocol, so a test double needs it.
- `Model.create_table()` and `Model.drop_table()` commit their DDL when no `transaction()` block owns the connection.
- `ConnectionPool.release()` rolls back before re-queueing, probes with `SELECT 1` and drops a connection that fails, and raises `ValueError` for a double release or a foreign connection.
- `transaction()` belongs to the thread that opened it; a second thread gets `RuntimeError`. A nested rollback releases its savepoint, and a failing rollback no longer replaces the caller's error.
- `async_transaction()` picks driver or statement transaction control the way `transaction()` does, through the new `driver_transaction_control()`, so psycopg2 no longer refuses the explicit `BEGIN`. Autocommit and legacy sqlite3 keep the statements.
- A rehearsal runs inside one pinned transaction via `execution.pinned_transaction`. On DuckDB each cursor is its own session, so rehearsed DDL used to commit as it ran and the closing rollback took nothing back.
- A rehearsal records a row for every start point as well as every end point, so `up(target=A)` then `up(target=B)` no longer demands a rehearsal it already proved. Building the keys costs one pass instead of re-hashing every prefix.
- The advisory lock result is checked before the run starts. MySQL `GET_LOCK` and MSSQL `sp_getapplock` signal refusal in their return value; both migrators now raise `MigrationError` through the new `migration_lock_problem()`.
- `script()`, `status()`, `statuses()`, `pending()`, `validate()`, and `plan` no longer create or upgrade the tracking table. They read through the new `read_applied_records()`, and a database without the table reports every migration pending.
- `down()` checks the whole revert window before reverting anything, so a run no longer stops half reverted on an edit or a missing down step it knew about at the start.
- `AsyncMigrator.down()` reverts a migration generated from the models; it read the fetch result wrong and raised "not registered with this migrator".
- A generated migration that failed no longer joins the registered list, so a long-lived migrator does not repeat its SQL on the next `up()`.
- The migration's own error survives a failed switch back to transaction control. A refused switch leaves the connection in autocommit; open a new connection to get transaction control back.
- The statement splitter takes a semicolon followed by a comment, so `...); -- note` no longer glues the next statement onto that one.
- The migration file naming check reads every file in the directory, so a typo'd extension such as `.sq` raises instead of loading nothing. Subdirectories, dotfiles, and editor copies are passed over.
- The destructive scan labels `DELETE FROM`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, and `DROP SCHEMA ... CASCADE`. A token pass keeps a `--` inside a string literal from hiding a drop, and a drop named inside quotes is no longer labelled.
- The `plan` command keys its guard verdicts by the normalized statement, so a custom guard's verdicts reach `plan --json` instead of an empty list.
- A result set that repeats a column name raises the new `AmbiguousColumns` error naming the columns, instead of the last value silently winning in every row dict. The check runs everywhere a row dict is built, sync and async.
- Attribute access on a model instance raises for a column the row does not carry, instead of answering the column name string, so `hasattr()` tells whether a field was loaded. Class access is untouched.
- The model registry no longer resolves a shared name to the wrong class. A string reference resolves through the module that declares the relation and raises `ValueError` naming every candidate when that fails.
- Views stay out of the shared `information_schema` read on MySQL, MariaDB, SQL Server, DuckDB, Presto, and Athena, so a view no longer diffs as an undeclared table or draws a `DROP TABLE`.
- MySQL `MODIFY` and SQL Server `ALTER COLUMN` restate the whole column through a new `ColumnState`, so an alter no longer drops NOT NULL, the default, the identity property, or the comment. Down steps carry the same fix.
- Postgres foreign keys are read from `pg_constraint`, so two same-named keys on different tables in one schema no longer cross-multiply into a spurious drop and re-add.
- Columns compare by their lowercased names, and the diff and the step generator share one type-change predicate, so a nullability-only drift no longer regenerates the type and truncates a MariaDB `datetime(6)`.
- `normalize_check()` also strips identifier quoting and operator spacing, so a check the engine rewrote compares equal to its declaration. A call keeps its parentheses, so two different checks stay different.
- `normalize_default()` reduces `nextval(...)` to `None`, strips outer parentheses only when they pair, and covers a cast with a length such as `::character varying(255)`.
- New tables are created in dependency order. Where the engine takes `ALTER TABLE ADD CONSTRAINT`, every foreign key follows `CREATE TABLE` as its own statement, so tables may point at each other. The ordering walk survives thousand-table chains.
- The table rebuild is kept to dialects that can run it. The new `rebuild_strategy()` makes Presto and Trino raise `DialectError` with a hand-written recipe instead of failing on the first statement.
- A SQLite rebuild of a referenced table runs between `PRAGMA foreign_keys = OFF` and `ON` with `transactional=False`, since SQLite ignores the pragmas inside a transaction. A failed step leaves enforcement off on that connection; the docs say how to restore it.
- The refusal of a new NOT NULL column with no default and no backfill runs before the rebuild path, refuses only while the table contains rows, and counts an unreadable row probe as rows.
- SQLite pragmas quote the table and index name, so a name with a space or a double quote reads.
- Each Postgres catalog query runs inside a savepoint, released after a rollback, so one missing view no longer aborts the whole read. The async read stops asking once the connection refuses savepoints.
- Athena records a comment change as a diff note instead of stopping the run, since Athena refuses the change in place. A hand-written `set_column_comment` step still raises.
- Athena `TBLPROPERTIES` keys and values escape their quotes.
- SQL Server `sp_rename` parses the bracketed path into segments and passes only the final segment as the new name, so quoted and schema-qualified names work.
- The live schema is read once per `autogenerate()` run instead of twice, and column comments are selected beside the other column data instead of in a second full read.
- The CLI removes the `sys.path` entry it added by value, not by position, so a config module that prepends its own directory keeps that entry.
- A compiler override written before the render context keeps working. The `Compiler` base class wraps the old signature at class creation, including `staticmethod` and `classmethod` overrides.
- `GuardBlocked([])` builds its message instead of raising `ValueError` over an empty `max()`.
- A capitalized join spelling such as `LeftJoin` resolves, and an unknown join-shaped name raises `AttributeError` so `hasattr()` works.
- The docs say a row count of `-1` from an async write means the driver reported no count; asyncpg does this for batched inserts. `returning()` gives an exact count.
- The matrix runner runs the container-free targets when compose fails, instead of reporting sqlite, duckdb, and athena as not started.
- The README links to the schema guide with the site URL, so the link works on GitHub and PyPI.

### Changed

- Every parameter after `allow_out_of_order` on `Migrator.up()` and `AsyncMigrator.up()` is keyword-only, so a positional call written for an earlier release raises `TypeError` at once. `rehearse()` keeps its signature.
- `Migration` raises `ValueError` when a checksum is given on SQL, statement-list, or ddl steps, which hash themselves; a pinned checksum hid edits from validation. Callable steps still take one. Run `repair()` if a stored row no longer matches.
- `down()` refuses a migration whose checksum no longer matches its tracking row, as `up()` already did. `allow_changed`, and `sustained down --allow-changed`, revert with the down step as it stands.
- `down()` refuses a revert count below 1. A negative `--steps` used to revert everything but the oldest and exit 0; a count of 0 still reverts nothing.
- A run that includes `DELETE FROM`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, or `DROP SCHEMA ... CASCADE` needs a passing rehearsal row before `migrate` applies it, or `--unrehearsed`. A plain `DROP SCHEMA` still passes.
- `no_lock_without_timeout()` reads the run in order, so a `SET lock_timeout` written after an `ALTER TABLE` no longer excuses it.
- An undeclared check read on MySQL, MariaDB, SQL Server, or DuckDB becomes a diff note rather than a refusal, since engine rewrites make the comparison unreliable. `allow_drops` still drops it.
- Postgres, SQL Server, and DuckDB reads cover the connection's schema plus every schema the models declare, instead of every non-system schema, since the snapshot keys on bare table names. Presto and Trino stay unscoped. MySQL widens from `DATABASE()`.

## 2.23.1

### Fixed

- Athena parameterized queries execute: the placeholder is now `?` instead of `%s`, which pyathena's pyformat style could not take as a tuple. Set `pyathena.paramstyle = "qmark"` (pyathena 3 or later) so the tuple travels as native execution parameters.
- Athena DDL quotes identifiers with backticks for the Hive parser, through the new `quote_ddl_identifier` compiler hook. Queries and `MERGE` keep double quotes for the Trino engine.
- Every Athena string column renders `STRING`, which Iceberg tables need. The new `normalize_diff_type` hook folds the reported `varchar` back, so the column never drifts against its own DDL.
- Athena execution parameters travel as strings through the new `prepare_execution` hook: numbers via `str()`, booleans as `true`/`false`, `None` as a literal `NULL`; binary raises `DialectError`. Pass `to_sql()` output through it if you execute it yourself.
- Athena introspection reads only the connection's schema. It read every Glue database in the account, which was slow and failed on any table with broken metadata.

## 2.23.0

### Added

- Column comments: every column definition takes a `comment`, stored where the engine has a place for one. Introspection reads them back, diffs report a change, and `set_column_comment` covers hand-written migrations. Athena refuses a change after `CREATE TABLE`.

## 2.22.0

### Added

- `Binary()` in `sustained.schema` declares a bytes column: `BLOB` by default, `BYTEA` on Postgres, `VARBINARY(MAX)` on SQL Server, `BINARY` on Athena. Introspection folds the variants back, so no drift. MySQL treats it off-row: no unique key, no literal default.
- The **Covered** column on the [support page](https://sustained.tbmh.org/support) is proven: each cover maps to a module in `tests/integration`, and a contract test fails when `support.json` and the test classes disagree. Five covers: queries, writes, transactions, migrations, async.
- `matrix.py` gains a `<name>-latest` target per container database, running the newest vendor-supported release pinned in `support.json`.

### Fixed

- `transaction()` spells nested-transaction savepoints per dialect: `SAVE TRANSACTION` on SQL Server, and nesting on DuckDB raises `DialectError` before any statement is sent.
- `transaction()` works on the duckdb driver, which autocommits and gives each cursor its own session. A transaction now pins one cursor for every statement inside it, so a failed multi-statement migration rolls back.
- `async_transaction()` renders its transaction control through the dialect compiler the same way.
- sqlite3 connections in legacy transaction control get an explicit `BEGIN` from `transaction()`, so a rolled-back block no longer keeps its schema changes.
- A NOT NULL change with a `backfill` on DuckDB compiles to `SET DATA TYPE ... USING coalesce(...)`, because DuckDB refuses `SET NOT NULL` after an `UPDATE` in the same transaction.
- Plain indexes are read back on MySQL, MariaDB, SQL Server, and DuckDB, so a declared index no longer drifts on every plan. Indexes that back foreign keys never demand `allow_drops`.
- Set-operation members render without parentheses on the default dialect, which SQLite rejects, so `union()` and its siblings run there. A bare member with its own ORDER BY or LIMIT raises `DialectError`.

## 2.21.0

### Added

- `Enum(*values, name=...)` in `sustained.schema` declares a column over a named, ordered value list. Postgres and DuckDB create a named type, MySQL renders inline `ENUM(...)`, the default dialect and MSSQL render VARCHAR plus a CHECK. Presto and Athena refuse it.
- Migration generation covers enum types: `ALTER TYPE ... ADD VALUE` on Postgres (irreversible; PostgreSQL 12 is the floor for rehearsing it), a restated `MODIFY COLUMN` on MySQL, a re-created CHECK elsewhere. Removing or reordering values refuses with a rebuild recipe.
- `Check(name, expression)` and `ForeignKey(name, columns, references, on_delete=, on_update=)` declare named table constraints in the new `tableConstraints` attribute, with composite columns and the five referential actions.
- Migration generation covers those constraints: a missing one generates `ADD CONSTRAINT` with a drop as its down step; changed and undeclared ones are gated by `allow_drops`. SQLite routes constraint changes through its table rebuild.
- `sustained.ddl` provides typed steps for hand-written migrations, from `create_table` to a raw `sql()` escape hatch. A step renders through the dialect compiler at run time, so one migration serves every dialect, and its checksum hashes the operation rather than the rendered SQL.
- A `Migration` whose up step is all reversible ddl steps derives its down step, newest first. Any drop, `add_enum_value`, or `sql()` refuses and asks for an explicit down step or `down=None`.
- Postgres introspection gets a dedicated read: real foreign key targets, non-unique indexes, varchar lengths, precision and scale, CHECK expressions, and enum value lists. SQLite recovers constraint names from `sqlite_master`; MySQL parses inline enums.

### Changed

- `DROP TYPE` and `DROP CONSTRAINT` (with `DROP CHECK` and `DROP FOREIGN KEY`) count as destructive: `plan` labels them, `no_drops()` blocks them, and the rehearsal gate covers them. Index and key drops still pass.
- `supports_constraints()` is `False` on Presto, which enforces none. Declared table constraints raise `DialectError` there and on Athena, and the tracking table renders without constraints on both.
- The row a rehearsal writes is a "rehearsal row" throughout, in place of "receipt": `rehearsal_key()` and the `REHEARSAL_*` constants. Stored tables, columns, and keys are untouched, so old rows still open the gate.
- `sustained rehearse` prints `rehearsal row recorded` where it said `receipt`; a script matching that text needs updating.

### Deprecated

- `receipt_key()`, `RECEIPT_PASSED`, `RECEIPT_FAILED`, and `RECEIPT_OVERRIDE` raise `DeprecationWarning` naming their replacements and go away in 3.0.

## 2.19.0

### Added

- A written [support policy](https://sustained.tbmh.org/support). `runs` means the integration suite applies migrations to a real server; `builds` means the SQL compiles under unit tests. The page also states the Python floor, the deprecation path, and what each version number promises.
- That list lives once, in `support.json`. `sync_support.py` renders the page from it, and a pre-commit hook fails when they disagree or a claim has no test module or compose service behind it.
- An integration suite in `tests/integration/`: one shared body applies the models, diffs, migrates, reverts, rehearses, validates, repairs, contends for the advisory lock, and round-trips a query on every server. `SUSTAINED_TEST_STRICT=1` turns skips into failures.
- `matrix.py` runs that suite: it starts servers from `docker/compose.yaml`, runs each module, and prints one line per server. Exit 0 clean, 1 failure, 2 waiting. A set connection variable uses that server and starts no container. Athena runs in your own AWS account.
- `Compiler.compile_create_table()` renders the whole CREATE TABLE statement, so a dialect that spells the if-missing check differently overrides one method.

### Fixed

- Tracking table columns quote through the dialect compiler. `generated` is reserved in MySQL, so the column probe read it as missing and every run tried to add it again.
- The MySQL advisory lock waits with a one-year timeout rather than a negative one, which MariaDB answers with NULL, so the lock was silently absent.
- SQL Server creates the tracking tables behind `IF OBJECT_ID(...) IS NULL`, since T-SQL has no `CREATE TABLE IF NOT EXISTS`.

## 2.18.0

### Added

- `Dialects.MYSQL` compiles for MySQL and MariaDB: backtick quoting, `%s` placeholders, `ON DUPLICATE KEY UPDATE` upserts, `AUTO_INCREMENT`, `MODIFY COLUMN`, a `GET_LOCK` advisory lock, and `for_update()` with `SKIP LOCKED` and `NOWAIT` on MySQL 8.0.
- Column types render in the spelling `information_schema` reports back, so no drift: `INT`, `TINYINT(1)` for `Boolean`, `DOUBLE`, `DECIMAL`, and `DATETIME` for `Timestamp`, whose `TIMESTAMP` alternative stops in 2038 and converts time zones.
- MySQL introspection reads `column_type` rather than `data_type`, scopes every query to `DATABASE()`, and matches schemas as well as names in the constraint join.
- MariaDB stores a `Json()` column as `longtext` with a `json_valid` CHECK; the read restores the JSON type so the column does not drift. MariaDB before 10.2.22 has no `check_constraints` view and does drift.
- `Compiler.supports_transactional_ddl()` reports whether rolled-back DDL really goes away. MySQL is the first engine where it differs from `supports_transactions()`: rows roll back, DDL commits as it runs.
- `Compiler.inline_references()` reports whether an inline `REFERENCES` clause creates a foreign key, with `compile_add_foreign_key()` and `compile_drop_foreign_key()` for the dialects that say no.

### Changed

- Schema reading moved from `sustained.autogenerate` to `sustained.introspect`. Every name re-exports from the old module, so imports keep working, and `type_params` is now public.
- Default normalization drops an empty argument list, so `current_timestamp()` and `CURRENT_TIMESTAMP` compare equal. Type normalization gained the `*TEXT` variants; `TINYINT` stays out, since `TINYINT(1)` is MySQL's boolean.

### Refused

- `rehearse()` refuses MySQL against the real database, since its rollback takes nothing back. Pass `scratch=True`, or define `get_rehearsal_connection()` for the CLI. Recovery after a half-failed run is `repair()`.
- `returning()` raises on MySQL, including against MariaDB, which supports it; SQL only one of the two servers accepts is worse than neither. Use a second query or `LAST_INSERT_ID()`.
- `STRING_AGG` raises rather than translating to `GROUP_CONCAT`, whose separator is a keyword and not a second argument.
- A `Text()` or `Json()` column takes neither a unique key, which MySQL wants a prefix length for, nor a literal `DEFAULT`, which it refuses.
- An unsigned integer column has no `tableColumns` declaration, so one already in the database reports as drift no migration closes.

## 2.17.0

### Added

- The tracking table gains a `steps` column storing a generated migration's up and down statements as JSON, so `down()` can revert what a process never diffed. Older tables add the column on first use.
- `up(unrehearsed=True)` records what it waived: a rehearsal row with the outcome `override`, which never opens the gate for a later run.
- `migrate` exits 4 when a run that removes data has no passing rehearsal, which a pipeline can tell apart from a failure.
- `Migrator.drift()` and `SchemaDiff.outstanding()` take `ignore_changed_columns`.
- A block or missing rehearsal row on a generated migration names the registered migrations that already applied, on the exception's `applied` attribute.

### Fixed

- A SQLite table rebuild no longer drops undeclared columns, their data, and hand-made indexes; they cross the rebuild with type, nullability, uniqueness, and default. `allow_drops=True` still drops them, and an expression index still cannot cross.
- An index on an expression no longer crashes introspection; SQLite reports a null column name for one, so it is left out of the schema.
- `up(models=[...])` runs the out-of-order validation check again for hand-written migrations.
- A rehearsal with models and rename hints no longer raises while checking that the models landed; the check runs without the hints and honours `ignore_changed_columns`.
- A rehearsal no longer blames its leftovers on the down steps that did reverse; the comparison runs only when every versioned migration in the run reversed.
- A rehearsal applies the generated migration before the repeatables, matching `migrate`.
- A scratch rehearsal records rows for the shorter target sets, so `migrate --target` is not refused for statements the scratch run proved.
- The plan footer no longer says `run: sustained rehearse` after a rehearsal recorded its row, and no longer computes verdicts over drops `migrate` never generates.
- `no_lock_without_timeout()` is Postgres only and anchored to a `SET` statement, so an update of a column named `lock_timeout` no longer fires it.
- Filter and write values accept `datetime`, `date`, `Decimal`, and `bytes` under a strict type checker.
- A sync savepoint that failed to open no longer leaves the nesting depth one too high, which reused a savepoint name.

## 2.16.1

### Added

- `Connection` and `Cursor` in `sustained.types`, protocols listing the DB-API 2.0 methods Sustained calls, so any conforming driver connection matches.
- `Binding`, the `Union[Connection, ConnectionPool]` that `Model.bind()` and every `connection=` argument take.
- `SqlValue` and `RowValue` split database values by direction: a value going in is `object`, a value read back stays `Any`.
- `ColumnDescription` and `RelationTree` in `sustained.types`, and `JsonValue` in `sustained.cli`.
- Driver protocols in `sustained.aio`: `AsyncpgConnection`, `AsyncpgRecord`, `AiosqliteConnection`, and `AiosqliteCursor`.

### Changed

- Roughly 180 `Any` annotations were replaced with the types above and with model, schema, and introspection types. The `Any` annotations that remain each carry a comment giving the reason.
- Migration callbacks and callable steps are typed `Callable[[CallbackTarget], CallbackResult]`; a callable step on the async path may return an awaitable.
- `in_()` and `not_in()` accept any sequence of values, not only a `list`.
- `ConnectionPool` closes connections by calling `close()` directly; the DB-API requires the method.
- The builder stubs declare `render()`, `has_clauses()`, the `compiler` argument, and the method maps `QueryBuilder` reaches for.

## 2.16.0

### Added

- The query builder is generic over its model: `Show.query()` is a `QueryBuilder[Show]`, so `run()` is `List[Show]`, `first()` is `Optional[Show]`, and `arun()` and `afirst()` match. No cast needed.
- `WriteBuilder[Model]`, returned by `insert()`, `insert_from()`, `create_table_as()`, `update()`, and `delete()`; its `run()` types as the row count or the RETURNING rows. At run time it is the same class as `QueryBuilder`.
- `WriteResult` in `sustained.types`, the union a write returns.
- `QueryBuilder[Show]` works in a run-time annotation.

### Changed

- Argument positions that take any query are declared `QueryBuilder[Any]`, since the builder is invariant in its model.
- The generic types live in `builder.pyi` only; the running code is unchanged.

### Not covered

- The select list does not narrow the result: `select('id')` still types as the whole model, and `to_dicts()` values stay `Any`.

## 2.15.0

### Added

- Guards: rules over the statements a run would apply, returning a `Verdict(rule, verdict, statement)` of `block` or `warn` per objection. Both migrators take `guards=[...]`, and the CLI config module names them.
- `sustained.guards` ships five factories: `no_drops()`, `index_must_be_concurrent()` (Postgres only), `no_table_rewrite()` (warns), `no_lock_without_timeout()`, and `max_statements(n)`, plus `run_guards()`, `blocking()`, and `warnings_only()`.
- `up()` raises `GuardBlocked` on a blocking verdict before any statement runs and prints warnings on stderr. No flag waives a guard: fix the statement or take the rule out.
- `sustained plan` runs the guards and prints a `guards` section, one line per verdict; in `--json` a verdict rides on its statement object.
- Exit code 3 means a guard blocked a statement, from `plan` and `migrate`. Precedence is 1 (problems) over 3 (blocked) over 2 (pending).
- Both migrators take `callbacks=Callbacks(...)`, so `before_migrate`, `after_migrate`, and `on_error` reach library callers, not only the CLI.
- A `dialect` property on both migrators, and `run_statements()` and `check_guards()` in `sustained.migrations`.

### Changed

- `sustained migrate` hands its config module callbacks to the migrator; `after_migrate` now fires before the post-run drift report.
- `plan --json` statement objects gained a `guards` key, present and empty when unflagged.
- Guards run twice on a run that includes the model diff, since the generated statements arrive late; a warning already printed is not repeated.
- `rehearse` does not enforce guards, since it rolls back and blocking would stop an operator testing the statement they are fixing.

## 2.14.0

### Added

- A passing rehearsal writes one row in the new `sustained_rehearsals` table, keyed by a SHA-256 over the applied and run checksums. A failing rehearsal records the failure under the same key.
- `up()` refuses a statement that removes data — a DROP TABLE, a column drop, or a TRUNCATE — unless a passing rehearsal row covers that exact set. `up(unrehearsed=True)` and `sustained migrate --unrehearsed` apply anyway. An additive run is never gated.
- A rehearsal records a row for each shorter `--target` run that removes data, so one rehearsal covers the whole run and every target within it.
- `RehearsalRequired`, in `sustained.exceptions` and re-exported at the root, is what the refusal raises.
- `record_rehearsal()`, `rehearsal_outcome()`, and `rehearsed()` on both migrators, and `rehearsal_key(applied, run)` in `sustained.migrations`.
- `rehearse --json` gains `key` and `recorded`, and the plain report prints `rehearsal row recorded`.
- Both constructors and the CLI config module take `rehearsal_table`.

### Changed

- `rehearse()` returns a `Rehearsal`, a `list` subclass carrying `key`, `recorded`, and `ok`.
- `rehearse(scratch=True)` records nothing through the API. The CLI writes the row on the real database after a passing scratch run that applied everything pending there.
- `sustained plan` prints `run: sustained rehearse` when a pending migration removes data, since migrate would refuse it.
- Both Sustained tables are excluded from every diff against the models.
- `rehearsal_failed(result)` moved from `sustained.cli` to `sustained.migrations`.

## 2.13.0

### Added

- `Migrator.up(models=[...])` diffs the models against the database, applies the generated migration after everything else pending, and takes the diff options `plan()` takes. It replaces `sync()`. A target cannot combine with models.
- `sustained migrate` and `sustained rehearse` pass the config module's `models`, so the model diff reaches the shell. A targeted `migrate` applies registered migrations only.
- `Migrator.rehearse(models=[...])` rehearses the generated migration alongside the pending ones without registering it.
- A rehearsal reports what the schema said: `landed` says the models arrived, and `reversed` compares against a pre-run snapshot. `None` means unchecked, `[]` proved, a non-empty list names the trouble; either failure exits 1.
- Tables and columns are compared for `reversed`; indexes, constraints, and column defaults are not yet.
- `sustained rehearse --json`.
- `sustained migrate` re-reads the schema after a run when the config module names models and reports the differences left. A report, never a gate.
- `Migrator.drift(models)` returns what the models still ask for, one readable line each; objects the models do not declare are left out.
- `diff_snapshots(before, after)` and `async_introspect_schema(adapter, dialect)` in `sustained.autogenerate`.

### Changed

- `plan --json` reports each pending migration's `statements` as objects carrying the SQL and a destructive flag, rather than a count. `PendingSummary.statements` became `PendingSummary.sql`.
- The generated diff no longer refuses objects the models do not declare, since hand-written migrations create such objects. Drops still need `allow_drops=True`, and `ignore_undeclared=False` restores the refusal.
- The tracking table gained a `generated` column marking rows a model diff wrote, added in place on first use.
- `sustained plan` prints `run: sustained migrate` for both pending work and drift; a drops-only drift section says migrate does not generate drops.
- Schema introspection is one query plan that the blocking cursor and the async adapter each drive, degrading to column-only data where constraint views are missing.

### Deprecated

- `Migrator.sync()` raises a `DeprecationWarning` and delegates to `up(models=[...])`. It goes away in 3.0.

## 2.12.0

### Added

- `withGraphFetched()` takes a dotted path such as `'shows.tickets'`, one batched query per level with `WHERE fk IN (...)`, so a deeper graph never becomes a query per row. Shared prefixes load once, and an unknown segment raises at build time naming it.
- Async eager loading covers link-table relations and dotted paths beyond the first segment. The sync loader split into a planner and an attacher both paths call.
- `async_transaction()` nests through ANSI savepoints, matching `transaction()`.

### Fixed

- The type stubs describe the join and clause methods the runtime accepts: `whereRaw`, `havingRaw`, `fullJoin`, `crossJoinRelated`, and the rest were missing, and `outerJoin` never existed. A test compares each stub against the runtime in both directions.
- `LENGTH` is registered once; the second registration, carrying the T-SQL `LEN` spelling, overwrote the first.
- `IntrospectedTable` and `FunctionMetadata` default their mapping fields to read-only empty mappings, so one instance's mapping cannot become another's.

## 2.11.0

### Fixed

- `repair()` no longer rewrites the stored checksum of a changed repeatable, which cancelled the re-run the change had scheduled. Failed-attempt rows are still removed.
- A malformed placeholder marker such as `${my-key}` or an unclosed `${key` raises `ValueError` naming the file, instead of passing through as raw SQL. Applies only when a placeholders mapping is given.
- `rehearse()` reads validation state, pending migrations, and applied records inside the advisory lock, so a concurrent migrator cannot apply between the read and the rehearsal.
- `AsyncMigrator.rehearse()` refuses a connection in autocommit mode, as `Migrator.rehearse()` already did.
- Tagging an exception with its migration id no longer raises on exception types that reject new attributes.
- `sustained plan` prints `run: Migrator.sync(models)` when it finds model drift, which `migrate` does not close.

### Changed

- A targeted `up()` no longer runs the repeatables, which may depend on migrations past the target; the next full `up()` runs them.
- The destructive scan labels a column drop written without the COLUMN keyword, as MySQL allows.
- The refusal message for rehearsing a non-rehearsable dialect mentions `scratch=True` for library callers.
- The docs cover the default dialect's place on the rehearsable list and scratch databases that keep objects between runs.

## 2.10.0

### Added

- `sustained rehearse` and `Migrator.rehearse()`: apply every pending migration, run the down steps back down, and roll it all back, so the database ends where it started. Exits 1 when a step failed. `AsyncMigrator.rehearse()` is the same on an adapter.
- Only databases whose schema changes roll back may rehearse: SQLite, Postgres, and DuckDB. Autocommit connections and open `transaction()` blocks refuse too. A config module's `get_rehearsal_connection()` sends the rehearsal to a scratch database instead.
- `Compiler.begin_transaction_sql()` and `rollback_transaction_sql()`: explicit statements the rehearsal uses, since drivers disagree on when a transaction exists. Engines without transactions return `None`.
- Config module callbacks around `sustained migrate`: `before_migrate(connection)`, `after_migrate(connection, applied)`, and `on_error(connection, migration_id, error)`. Only `migrate` calls them.
- `Migrator.connection` and `AsyncMigrator.adapter` properties.

### Changed

- A failing statement, a connection that will not open, or a directory that will not load prints as an error line on the command line instead of a traceback.

## 2.9.0

### Added

- `sustained plan`: one screen with the pending migrations, the problems `validate` would report, and the drift against the config module's `models`, drops included. Exits 0 current, 2 pending, 1 problems. Note argparse also exits 2 on a usage error.
- Destructive labels: the new `sustained.analysis` module labels drops and truncates in the plan, via `destructive_statements(sql)` and `summarize(migration, state)`. The label informs the operator; nothing is blocked.
- `--json` on `status`, `validate`, and `plan`: one JSON object on stdout, exit codes unchanged. `plan`'s `drift` is null rather than empty when no models were named, separating "not compared" from "no gap".

## 2.8.0

### Added

- Repeatable migrations: a `<id>.repeat.sql` file, or `Migration(id, up, repeatable=True)`, re-runs whenever its checksum changes, for views, functions, and seed data. `down()` never reverts them, and `baseline()` records them at their current checksum.
- `statuses()` on both migrators: (id, state) pairs with `applied`, `pending`, and `changed`, which the CLI `status` command prints.
- Placeholders in SQL migration files: `${key}` fills from `load_migrations(placeholders=...)` or the config module. A missing key raises `ValueError`, `$${` escapes, and with no mapping files load untouched. Substitution runs before checksums compute.

### Changed

- `pending()` also returns repeatables whose checksum changed, since the next `up()` will run them.
- `load_migrations()` rejects a `.sql` file only when it matches none of the three suffixes.

## 2.7.0

### Added

- Migrations as SQL files: `load_migrations(directory)` pairs `<id>.up.sql` files with optional `<id>.down.sql` files, splitting statements at line-ending semicolons. Empty files, orphaned down files, and misnamed `.sql` files raise `ValueError`.
- `baseline(target)` on both migrators records migrations up to the target as applied without running them, for adopting a database whose schema already matches.
- `Migrator.plan(models, ...)`: the migration `sync()` would generate, without registering or applying it, or `None` when the schema is current.
- A command-line runner: the `sustained` console script and `python -m sustained` drive a `Migrator` from a config module, with `status`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline`.

## 2.6.0

### Added

- The tracking table records a sequence number, a SHA-256 checksum of the up statements, execution time, and a success flag; apply order reads from the sequence. Older tables upgrade in place on first use; on Athena the upgrade needs an Iceberg tracking table.
- `validate()` on both migrators raises `MigrationError` on failed attempts, applied ids the migrator does not know, checksum mismatches, and pending migrations ordered before applied ones.
- `repair()` deletes rows left by failed attempts and rewrites drifted or null checksums.
- On engines without transactions, a failing step writes a failure row that blocks the next `up()` until repaired.
- Migration runs take an exclusive advisory lock named after the tracking table: `pg_advisory_lock` on Postgres, `sp_getapplock` on MSSQL.
- `Migration` accepts an explicit `checksum` for callable steps, and `migration_checksum()` exposes the value validation compares.
- `applied_records()` returns the tracking rows with sequence, checksum, and success flag.

### Changed

- `up()` validates before running. `validate=False` skips the checks; `allow_out_of_order=True` accepts a pending migration ordered before an applied one, which earlier versions applied silently.

### Fixed

- The tracking table upgrade backfill touches only the columns the current run added and only rows where they are still null, so a recorded failed attempt survives an interrupted earlier upgrade.

## 2.5.0

### Added

- AWS Athena dialect (`Dialects.ATHENA`): Presto's query behavior with `%s` placeholders matching pyathena, MERGE upserts on Iceberg tables, and Athena's type spellings (INT, STRING, DOUBLE, DECIMAL; JSON maps to STRING).
- `TableOptions(location, partitioned_by, properties)`: storage clauses declared as a model's `tableOptions`, rendered as PARTITIONED BY, LOCATION, and TBLPROPERTIES on Athena. Other dialects raise when options are set.
- Athena DDL: `ADD COLUMNS` for added columns, `CHANGE COLUMN` for Iceberg type widenings. Constraints, indexes, renames, nullability changes, RETURNING, and temporary CTAS raise `DialectError` with directions.
- Migrations on engines without transactions: each step runs bare on Athena, never calling rollback. Both migrators accept `tracking_table_options` and create the tracking table without constraints on constraint-free engines.
- The function registry recognizes Athena wherever it recognizes Presto, including `NOW()` and the `GETDATE()` translation.
- Schema diffing normalizes Athena's STRING type, so tables created from models diff clean.

## 2.4.0

### Added

- Constraint-aware introspection: primary keys, unique constraints, foreign keys, column defaults, and indexes read from SQLite PRAGMA tables or information_schema, with graceful degradation and system schemas filtered.
- Type and nullability changes generate migrations: in-place reversible `ALTER COLUMN` on Postgres (with `type_casts` USING hints), MSSQL, and DuckDB; an automatic table rebuild with row copy on SQLite.
- Rename hints: `renames={'table.old': 'new'}` and `table_renames` produce reversible RENAME statements (sp_rename on MSSQL) instead of destructive drop-plus-add.
- Declared indexes on models via `Index`, created with the table and diffed for additions, definition changes, and opt-in drops, all reversible.
- `backfill` on ColumnDef: NOT NULL adds and tightenings emit add-nullable, UPDATE, SET NOT NULL, or fold into the SQLite rebuild.
- Length and precision changes detected when both sides report them.
- Constraint notes: PK, FK, unique, and default drift reported in the diff, never auto-migrated.
- Offline scripts: `migration_sql()` and `Migrator.script()` render the SQL a run would execute for DBA review.
- `AsyncMigrator`: the migration runner on an AsyncAdapter with transactional application and awaited callable steps.

## 2.3.0

### Added

- Schema autogeneration: `diff_schema()` introspects the live database and reports missing tables, new columns, extra objects, and changed columns with a readable `summary()`. Type comparison round-trips through each dialect's own mapping.
- `autogenerate()` builds a `Migration` from the diff. Additive steps are reversible; drops need `allow_drops=True` and carry no down step; changed column types block unless ignored; NOT NULL adds without defaults and primary key adds are rejected.
- `Migrator.sync(models)`: diff, generate, register, and apply in one idempotent call. `Migrator.down_to(id)` reverts newest-first until the target is the most recent applied migration.
- Compilers render `ADD COLUMN` and `DROP COLUMN` statements, with the T-SQL `ADD` spelling on MSSQL.

## 2.2.0

### Added

- Typed column definitions: models declare `tableColumns` with `Integer`, `BigInteger`, `String`, `Text`, `Boolean`, `Float`, `Numeric`, `Date`, `Timestamp`, and `Json`, including composite primary keys, defaults, unique constraints, references, and autoincrement.
- Model-driven DDL: `create_table_sql()`, `create_table()`, and `drop_table()` with per-dialect type mapping and identity syntax. DuckDB and Presto raise for autoincrement.
- Migration runner: ordered `Migration` objects with up/down steps (SQL, statement lists, or callables), a self-creating tracking table, transactional application, stop-after targets, and newest-first reverts. `create_table_migration()` derives create/drop pairs.
- `ConnectionPool`: thread-safe, lazy, bounded pooling for DB-API connections. `Model.bind()` and all execution entry points accept a pool; transactions pin one checked-out connection to the thread.
- Async execution: `arun()`, `afirst()`, and `ato_dicts()` through an adapter interface with `DbApiAsyncAdapter`, `AiosqliteAdapter`, and `AsyncpgAdapter`, plus `Model.bind_async()` and `async_transaction()` with ContextVar pinning.

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

- String arguments to `select_func()` and the dynamic function methods are now column references, not string literals. Wrap literal values in `Literal()`.
- Operators passed to `where()` and `having()` are validated against an allowlist; unrecognized operators raise `ValueError`.
- `top()` raises `DialectError` on dialects other than MSSQL. It previously disappeared from the query without warning.
- On MSSQL, `limit()` and `offset()` raise `DialectError` when the query has no `ORDER BY`, because T-SQL rejects OFFSET/FETCH without one.
- `whereILike()` compiles to `LOWER(col) LIKE LOWER(pattern)` on dialects without native ILIKE. Postgres keeps native `ILIKE`.
- Booleans render as `TRUE`/`FALSE`, or `1`/`0` on MSSQL, instead of the Python words.
- Duplicate CTE aliases with different definitions raise `ValueError` instead of silently keeping the last one.
- `update()` and `delete()` refuse to render without a `where()` clause.
- Empty `whereIn()` lists raise `ValueError`.
- Column references in WHERE, HAVING, and GROUP BY clauses quote per dialect when they are plain identifier paths.
- `with_()` requires a `QueryBuilder` and renders it lazily; later changes to the CTE subquery reach the output.
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

- Dialect-specific query compilation: a query builds once and compiles for a chosen dialect, starting with the default, PostgreSQL, MSSQL, and Presto compilers.
- A function registry with per-dialect validation: `select_func()` and the fluent function methods raise `DialectError` at build time when the dialect does not support the function.

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
