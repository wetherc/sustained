---
layout: default
title: Changelog
description: "Release notes for every version of Sustained, the Python ORM, query builder, and schema migration tool."
---

Every released version of Sustained, newest first, grouped by major version. The same text lives in `CHANGELOG.md` in the repository; this page is generated from it.

Version numbers follow semantic versioning. A major version marks a change that can break working code. A minor version adds new features. A patch version fixes a defect without changing public API signatures or introducing new functionality.

## 2.x

### 2.28.1

#### Changed

- The sdist and the wheel include a `THIRD_PARTY_NOTICES` file with the MIT copyright and permission notices of Objection.js and Knex.js, whose model, relation, and query builder APIs Sustained adapts. The wheel puts the file in `dist-info/licenses` next to `LICENSE`.
- `LICENSE` names the copyright holder, Christopher Wetherill.
- The README and the `create_model()` docstring show a `Show` model that belongs to a `Venue`, in place of the example from the Objection.js relation guide.

#### Fixed

- On Athena, a string parameter whose literal is longer than 1,024 characters travels as several parameters joined with `||`, each one within the limit of Athena's API. The migrator's tracking row stores a migration's steps as JSON, which is usually longer than that, so a migration on Athena no longer runs without a tracking row and a failed one records its failed attempt.

### 2.28.0

#### Added

- `sustained.raw(sql)` writes raw SQL without a `QueryBuilder` in scope and returns the same `Expression` that `QueryBuilder.raw()` returns. A `raw()` value works in every column and value position and renders as written.
- `CaseExpression.when()` and `select_case()` take a `Predicate` built from `col()` as the condition, such as `(col("age") >= 18) & (col("name") == "x")`. Its columns take the dialect's quotes and its values render as inline literals. A string condition stays raw SQL.
- `ColumnDef.replace(**changes)` returns a copy of a column with the named attributes changed. It does not run the constructor checks again and raises `AttributeError` for a name that `ColumnDef` does not have.
- `AsyncAdapter.caller_autocommit()` reports whether the caller put the driver's connection in autocommit, and `AsyncMigrator.rehearse()` reads it to refuse a rehearsal in autocommit. The base class returns `False` and `DbApiAsyncAdapter` reads its connection, so a custom adapter has to override it to keep that refusal.
- `ColumnReference` includes `Subquery`, so a type checker accepts a subquery as the column of `where()`, `having()`, `join()`, `groupBy()`, and `orderBy()`. The column of `AggregateExpression` and the `partition_by` and `order_by` of `WindowExpression` and `select_window()` take `col()`, `raw()`, `Func`, `Literal`, and `Subquery` values, in any `Sequence`.

#### Changed

- A migration whose `ddl.create_index()` or `ddl.create_table()` step declares a DESC key part, a prefix length, or a `where` predicate hashes those details, so such a migration applied before this release reports as changed. Earlier releases left those details out of the statement, so check the index on the database, build it again if it lacks them, and then run `repair()`. A step with plain indexes keeps its checksum.
- A `ddl.create_index()` or `ddl.create_table()` step that declares a prefix length on a dialect other than MySQL, or a `where` predicate on MySQL, DuckDB, or Presto, raises `DialectError` when it renders, where it rendered the index without the detail. `script`, `migrate`, and `rehearse` of such a step fail until the step drops the detail that the dialect lacks.
- The `reversed` check of a rehearsal compares indexes on PostgreSQL, MySQL, MariaDB, SQL Server, and SQLite, by key columns, uniqueness, DESC key parts, prefix lengths, and partial-index predicate. It compares checks, UNIQUE constraints, and foreign keys on PostgreSQL, MySQL, MariaDB, and SQL Server. A down step that leaves an index or constraint behind, or restores it with other details, fails the rehearsal with `not reversed`. `diff_snapshots()` takes an optional `dialect` that adds these comparisons, and without it compares tables and columns only.
- On PostgreSQL and SQL Server, which store a partial index predicate in their own spelling, autogenerate reports a predicate that differs from the model as a constraint note and leaves the index in place, as it does for a check constraint. Write a predicate change on those engines as a hand-written migration. SQLite stores the predicate as written, so a predicate change there still drops and creates the index, and a change in columns, uniqueness, DESC key parts, or prefix lengths still drops and creates the index on every engine.
- A column string in `select()`, `where()`, `having()`, `orderBy()`, `groupBy()`, `distinctOn()`, `returning()`, aggregates, join ON conditions, and `col()` is a dotted path or a call on one column such as `'SUM(price)'`. Each part of a path is quoted for the dialect, so names such as `"Employee ID"` or `"prénom"` work. A part already in `"..."`, `[...]`, or `` `...` `` quotes takes the target dialect's quotes, so `'[Employee ID]'` works on PostgreSQL and `'dbo."a.b"'` names the column `a.b`. The function name of a call is written into the SQL as given, so a column string must not come from untrusted input. Check a column name or sort key from a request against an allowlist of column names before you pass it. The default dialect writes names bare, so it still raises `ValueError` for a part that is not a plain name, and a string with an empty part, such as `'a..b'`, still raises `ValueError` on every dialect.
- In a `select()` string, the text after ` AS ` is the alias and must be a plain name, or `ValueError` is raised. Put a column name that contains ` AS ` in quotes, such as `'"Cost AS Pct"'`.
- String arguments to `Func`, `select_func()`, and window functions follow the column string rule of `select()`. A string that is not a column name, such as a value without its `Literal()` wrapper, is quoted as a column on dialects that quote names, so the database rejects the query, where it raised `ValueError`. The default dialect still raises `ValueError`.
- `orderBy()`, `groupBy()`, `groupByRollup()`, `groupByCube()`, and `groupByGroupingSets()` raise `ValueError` for a `Literal` argument, because `ORDER BY 1` and `GROUP BY 1` name a select-list position and not the value 1. Use `raw('1')` for a position or `col()` for a column.
- Compiler errors for a feature that a dialect lacks use one wording, such as "The MySQL dialect does not support RETURNING.", with any explanation after it. The error names the dialect as "PostgreSQL", "SQL Server", "MySQL", "DuckDB", "Presto", "Athena", or "default". This covers DISTINCT ON, FOR UPDATE, table options, TOP, ROLLUP, CUBE, GROUPING SETS, RETURNING, CREATE TABLE AS, EXPLAIN, identity columns, upserts, named enum types, column comments, in-place column type and nullability changes, the Presto and Athena enum, CHECK, and foreign key refusals, and Athena temporary tables, table renames, and indexes. It also covers the index prefix length, DESC key part, and partial index refusals, such as "The PostgreSQL dialect does not support index prefix lengths. Index column 'a' declares one." and "The MySQL dialect does not support partial indexes. Index 'ix_a' declares a WHERE predicate." The exception type stays `DialectError`, so only code that matches the message text needs a change.
- The `where`, `having`, `join`, and `orderBy` methods, with their `and` and `or` forms, are real methods on `QueryBuilder` and the clause builders, so `dir()`, `hasattr()`, `help()`, and editor completion list them. `orWhere()` and the other `and` and `or` forms on an empty clause raise `RuntimeError` when called, not when the attribute is read. A raw join such as `join()` or `leftJoin()` raises `TypeError` for an unknown keyword argument, where it ignored the argument.
- `qualified_table_name()` and `joinRelated()` raise `ValueError` in place of `AssertionError` for a model with no `tableName`, so the check also runs under `python -O`.
- The impact recognizer counts square brackets as nesting in every reader, so a comma or keyword inside `x[...]` no longer ends an item, an option list, or a word search. A parenthesized group with no closing parenthesis reports "expected ')' at the end" as the unknown statement's reason, where it reported "the statement ends early", and a reader that fails its final check records the table it named as the unknown statement's table.
- The statement listener's duration for a multi-row insert does not include building the batch plan, and `ato_dicts()` calls the listener before the async adapter's scope closes.
- On a `Compiler` subclass, `escapes_percent()` returns `True` whenever `placeholder()` returns `"%s"`, so a third-party compiler with `%s` placeholders that does not override it doubles a literal `%` in parameterized SQL. The base `CREATE TYPE` and `DROP TYPE` methods render when `enum_strategy()` returns `"native"`, where they raised for a subclass that did not override them.

#### Deprecated

- `Column` gives a `DeprecationWarning` when it is built and goes away in 3.0. Use `raw()` for raw SQL or `col()` for a quoted column. Until then, `Column` works in every position that takes `raw()`.
- `sustained.migrations.plan_migration()` gives a `DeprecationWarning` and goes away in 3.0. Call `sustained.autogenerate.autogenerate()` instead.
- `sustained.execution.open_cursor()` returns a cursor as before, gives a `DeprecationWarning`, and goes away in 3.0. Use `cursor_scope()`, which also closes a cursor it opens.

#### Removed

- `sustained.migrations`, `sustained.introspect`, and `sustained.autogenerate` no longer re-export the underscore helpers of their submodules, such as `_tag_migration`, `_introspected_fk_sql`, `_diff_indexes`, or `_READ_SAVEPOINT`, and the rule profile packages `sustained.impact.rules.postgres`, `.mysql`, `.mssql`, `.sqlite`, and `.duckdb` export only `PROFILE` (or `MYSQL`, `MARIADB`, and `asserted_statements` for MySQL). Import a removed name from the submodule that defines it, such as `sustained.introspect.runner._READ_SAVEPOINT` or `sustained.impact.rules.mysql.locks.COPY_NONE`. Every public name of these packages stays importable from the package.

#### Fixed

- The `ddl.create_index()` and `ddl.create_table()` steps and autogenerate render a declared index's DESC key parts, MySQL prefix lengths, and partial-index `WHERE` predicate, where they rendered the column names only.
- The schema read reports DESC key parts, partial-index predicates, and MySQL prefix lengths on PostgreSQL, MySQL, MariaDB, SQL Server, and SQLite, so autogenerate finds a change in any of them. DuckDB reports none, since its catalog drops DESC and it has no partial indexes. MySQL 5.7 and MariaDB before 10.8 accept DESC but store an ascending index, so a declared DESC key part on those servers reads as changed.
- Autogenerate down steps that build an index again from the catalog, the drop and create of an index around `ALTER COLUMN`, and the SQLite rebuild of an undeclared index with no stored statement render the DESC key parts, prefix lengths, and predicate that the schema read reports, where they rendered the column names only. The predicate keeps the catalog spelling, such as `(a > 0)` on PostgreSQL. With `online=True` on a partitioned PostgreSQL table, `CREATE INDEX ... ON ONLY` and the concurrent build on each partition also render them.
- `Index(..., where="")` or a `where` of only whitespace means no predicate, so the index renders, compares, and hashes like a plain index, where it rendered `CREATE INDEX ... WHERE ` with no condition.
- The check and partial-index predicate comparison removes the casts that PostgreSQL adds, such as `::text`, and the parentheses around each operand of `AND` and `OR`, and reads SQL Server's `([a]>(0))` as `a > 0`, so a predicate such as `a > 0 AND b > 0` no longer reports as changed on every run. It no longer strips parentheses that do not match.
- On PostgreSQL and DuckDB, autogenerate writes `COMMENT ON COLUMN` for a new column that has a comment, where the next diff reported the missing comment as drift.
- `transaction()` sends `BEGIN` on a sqlite3 connection made with `connect(factory=...)` and on a pysqlite3 connection in legacy transaction control, so schema statements in the block roll back. `async_transaction()` over `DbApiAsyncAdapter` does the same.
- Outside a transaction block, `AsyncMigrator` over `DbApiAsyncAdapter` runs every statement of one migration step on one cursor, where it opened a cursor for each statement. On DuckDB each cursor is a session of its own, so a TEMP table, `USE`, or `SET` from one statement of a `transactional=False` step did not reach the next statement.
- A model that sets `tableSchema` or `database` but no `tableName` raises `ValueError` for `INSERT`, `UPDATE`, and `DELETE`, where it built SQL such as `DELETE FROM sales` that read the schema as the table. A `SELECT` renders without `FROM`, as for a model with no table.
- `col()`, `Func`, and `Literal` work as the column of `where()`, `having()`, aggregates, and window partitions and orders, and `col()` and `Func` work in `orderBy()` and `groupBy()`, where each raised `TypeError`. The select list accepts a `Literal`, and CASE results accept `col()` and `Func`.
- `insert()` and `update()` values bind a `Literal`, quote a `col()`, write `raw()` as given, and render a `Func` as a call, where they bound the Python object.
- Keys of `insert()`, `update()`, and `insert_from()`, conflict columns of `onConflict()`, and column lists of `merge()` written in another dialect's quotes, such as `'[Full Name]'`, take the target dialect's quotes. A quoted key kept its original quote characters inside the new ones, a quoted conflict column raised `ValueError`, and a quoted merge column was quoted twice.
- `insert()`, `update()`, `insert_from()`, `onConflict()`, and `merge()` raise `ValueError` that names both keys when two keys name the same column once their quotes come off, such as `"a"` and `'"a"'` or `"name"` and `"[name]"`. A multi-row insert compares the unquoted keys, so `[{"a": 1}, {'"a"': 2}]` writes one column `a`, where it raised `ValueError`.
- Join ON conditions quote names by the column string rule, so already-quoted parts such as `'"other"."a.b"'` are not quoted twice, and `col()` works on both sides.
- A `Subquery` as the column of `where()` or `having()`, in `orderBy()`, `groupBy()`, an aggregate, a window partition or order, or a join ON condition binds its values as parameters in `to_sql()`, where it wrote them inline. A `WITH` clause inside such a subquery joins CTE hoisting, and `str()` output is unchanged.
- The `where`, `having`, and `join` methods accept keyword arguments with the parameter names in the type stubs, such as `where(column_or_callable="a", op="=", val=1)` or `whereIn(col="id", vals=[1])`, where they raised `TypeError`.
- `where()`, `having()`, and `update()` take a `QueryBuilder` as the value and render it as a subquery, as `whereIn()` does, where they bound it as a parameter and `str()` raised.
- `lower()`, `upper()`, `trim()`, `length()`, `abs()`, `ceiling()`, `floor()`, `round()`, `mod()`, and `substring()` read one positional argument past their fixed arguments as the alias, as the type stubs declare, where `lower("name", "l")` rendered `LOWER(name, l)`. More positional arguments, or an alias given both ways, raise `TypeError`. A `None` length for `substring()` or `None` decimals for `round()` is left out of the call.
- `withGraphFetched()` reads the parent key from the right column when the relation's `from` names the parent table with its schema, such as `"zoo.owners.id"`. A relation that lacks `modelClass`, `join`, or the join's `from` or `to` raises `ValueError` naming the missing key, as `joinRelated()` does, where eager loading raised `KeyError`.
- `withGraphFetched()` builds its child queries in the parent query's dialect. A query built with `QueryBuilder(Model, dialect=...)` sent the query for the related rows unquoted and with `?` placeholders when the related model had no dialect of its own.
- `joinRelated()` and its variants raise `ValueError` that asks for `alias=` on a relation from a model to its own table. Without an alias, both sides of the ON condition named the same table, so the database rejected the join as ambiguous or compared each row with itself.
- On MySQL 8, the schema read removes the extra backslash escapes from a check clause with a string literal, where a down step that restored the check failed with a syntax error and the expression did not match the declaration.
- The SQLite schema read keeps the whole check expression when a quoted identifier such as `"a)b"`, `` `a)b` ``, or `[a)b]` contains a close parenthesis, where it cut the expression short.
- On MySQL, MariaDB, and SQL Server, two tables with indexes whose names differ only in case, such as `Idx_A` and `idx_a`, each report their own spelling, where both reported the first one.
- The default comparison removes the whole cast from a PostgreSQL default such as `'2020-01-01'::timestamp(3) without time zone`, where it left `without time zone` behind.
- After a run renames a table, the impact analysis reports a later `DROP INDEX` of an index the run created on it under the table's new name.
- Inside a transaction, a table that the migration renames with `ALTER TABLE t RENAME TO u`, `RENAME TABLE`, or `sp_rename` forms one transaction window, named by the last name that a lock on the table uses. It formed one window for each name, which gave a false `window.lock_order` warning that the migration blocks reads and writes on `t, u` at once and split the `window.held` levels of the table. The `windows` list in the report and the JSON output changes for such a migration.
- On DuckDB and SQLite, a `DROP INDEX a, b` with a `drop_index` intent uses the intent's table only for a statement that names one index, as on PostgreSQL, where it put every index that no read found on that table. Each such index gets the "(table of index X)" placeholder.
- `preflight()` keeps each statement's attached impact when only some statements of the run have one, as the impact guards do, where it analyzed the whole run again and dropped the attached impacts.
- `sustained.impact.recognizer.STATEMENT_KINDS` and the recognizer docstring list `attach_index`, which the recognizer gives for PostgreSQL's `ALTER INDEX ... ATTACH PARTITION`.
- The impact recognizer reads MySQL's `ANALYZE TABLE t`, with or without `NO_WRITE_TO_BINLOG` or `LOCAL`, in every dialect as an unknown statement on the table `t` with the reason "no rule reads ANALYZE TABLE", where the unknown statement named the keyword `TABLE` as its table. `VACUUM TABLE t` and `CLUSTER TABLE t` are unknown statements that name no table.

### 2.27.0

#### Added

- `orderBy()` takes `nulls='first'` or `nulls='last'` to place `NULL` values at that end of the order. PostgreSQL, SQLite, DuckDB, Presto, and Athena render `NULLS FIRST` or `NULLS LAST`. MySQL and SQL Server have no such clause, so the key renders as `CASE WHEN col IS NULL THEN 0 ELSE 1 END` in front of the column. Any other value raises `ValueError`.
- `Index` takes `where=` for a partial index predicate and accepts `IndexColumn(name, desc=False, prefix_length=None)` in place of a column name, so a model can declare a partial index, a DESC key part, or a MySQL prefix index. The compilers render them, and refuse a WHERE predicate on MySQL or a prefix length outside MySQL with `DialectError`.
- `AsyncMigrator.read_schema(models)` and `AsyncMigrator.sync(models)` mirror the `Migrator` methods of the same names. `sync()` gives the same deprecation warning on both migrators.

#### Fixed

- `whereNotIn()`, its `or` and `and` forms, and `col(...).not_in()` raise `ValueError` for a list with a `None` member. Such a list renders `x NOT IN (..., NULL)`, which is never true, so the query returned no rows.
- On SQL Server, an `UPDATE` or `DELETE` whose subquery defines a CTE renders one `WITH` clause in front of the statement, where the subquery rendered its own `WITH` inside parentheses and the server refused it. Other dialects keep the `WITH` inside the subquery.
- Athena execution parameters travel as SQL literals: a string is quoted with its inner quotes doubled, a date renders as `DATE '...'`, a datetime as `TIMESTAMP '...'`, and a boolean as `TRUE` or `FALSE`, where a string or date was sent bare and the engine read it as an identifier or refused it.
- On SQL Server, the impact analysis drops `GO` batch separators before it reads a statement, so a statement that ends in `GO` is recognized instead of reported as unknown. Text with more than one batch still reads as unknown.
- `ddl.drop_column()` and the generated drop of an extra column on SQL Server first drop the default constraint the engine keeps for the column, by looking its name up in `sys.default_constraints`, so the `DROP COLUMN` no longer fails on a column that has a default. A `ddl` step hashes its operation and arguments, not the SQL it renders, so the checksum of a migration that already uses `ddl.drop_column()` does not change. A migration written as SQL text that drops the constraint by hand keeps working and keeps its checksum.
- A SQLite table rebuild writes back each index the model does not declare from the `CREATE INDEX` statement in `sqlite_master`, so an expression index such as `CREATE INDEX ix ON t (lower(name))` and the `WHERE` clause of a partial index come through the rebuild as written.
- Autogenerate reports a difference in an index's partial predicate, key part direction, or prefix length as drift when the introspection read reports those details on `IntrospectedIndex`.
- A PostgreSQL type change on a column that has a default drops the default before `ALTER COLUMN ... TYPE` and sets the model's default after it, where the server refused the change when the stored default did not cast to the new type. A column without a default generates the same statements as before.
- The default comparison in the schema diff keeps the case of a string literal, so a model default of `'YES'` on a column whose database default is `'yes'` is reported as drift. Keywords such as `NULL`, `TRUE`, and `CURRENT_TIMESTAMP` still compare without regard to case.
- The SQL Server schema read reads each column's `MS_Description` extended property as its comment, so a comment that differs between a model and the database is reported as drift. SQL Server has no statement to set a column comment, so the diff records the difference as a note and does not generate a migration for it.
- A column rename hint for an enum column on SQL Server drops the `ck_<table>_<old>_enum` check before the rename and adds `ck_<table>_<new>_enum` after it, in the same migration, where the check kept the old column's name and the next diff reported it as missing. On SQLite the rename rebuilds the table, which writes the check under the new name. `ddl.rename_column()` takes an optional `column` declaration and does the same for an enum column; a step without it renders and hashes as before.

### 2.26.0

#### Added

- `sustained impact`, `Migrator.impact()`, and `AsyncMigrator.impact()` report, for each statement a run would apply, the tables it locks, what the lock blocks, the work it does (catalog change, scan, row writes, index build, or rewrite), and how long the lock lasts, with the safer form of the statement where one exists. `--json` prints the report as one object, and the guide is [Statement impact](https://sustained.tbmh.org/impact).
- The impact analysis covers PostgreSQL, InnoDB tables on MySQL 8.0.19 and later and MariaDB 10.6 and later, SQL Server 2012 and later, SQLite 3.35 and later, and DuckDB 1.0 and later, and reads the server's version, settings, table sizes, and schema so that blocking work on a large table is `danger`. `sustained.impact.analyze(statements, dialect)` analyzes statements without a connection, and a statement the analysis cannot read is reported as unknown, never as safe. Each statement is read against what the earlier statements of the run did, such as a table created, renamed, or attached as a partition, a domain created, or a `ROLLBACK`, and a fact the server read missed, such as a table's size or its partitions, is named in a finding and counted by the guards as unknown.
- `sustained plan` lists the statements with a `warn` or `danger` finding in an `impact` section, and `sustained script --annotate` and `script(direction, annotate=True)` on either migrator print each statement's impact above it as SQL comments.
- The guards `max_blocking()`, `no_rewrite()`, `lock_timeout_required()`, and `no_unknown_impact()` in `sustained.guards` block statements by their analyzed impact instead of their text. `up()` and `sustained migrate` put each statement's impact on `MigrationStatement.impact` for custom guards, and print each `danger` finding on stderr when no configured guard reads impact.
- `sustained rehearse --trace` and `rehearse(trace=True)` on either migrator run each statement on its own on PostgreSQL, MySQL, MariaDB, and SQL Server, record the locks, rewrites, and index builds the server shows, and report each difference from the prediction as an `impact.mismatch` finding. On MySQL and MariaDB the scratch rehearsal also finds the `ALGORITHM` and `LOCK` clause the server accepts.
- `sustained impact --live`, `impact(live=True)`, and `preflight()` on either migrator list the sessions a run's statements would wait behind on the server now, and the transactions open at least `--older-than` seconds, 60 by default. `up(preflight="warn")` prints them before any migration applies, `up(preflight="refuse")` raises `PreflightBlocked` instead, and `sustained migrate --preflight warn|refuse` does the same and exits 5 on `PreflightBlocked`.
- `assert_algorithm=True` on MySQL and MariaDB, on `plan()`, `up()`, `rehearse()`, and `impact()` of either migrator and on `autogenerate()`, writes the predicted `ALGORITHM` and `LOCK` clause on each generated statement predicted to run `INSTANT` or with `LOCK=NONE`, so the server refuses them instead of running a slower algorithm or taking a stronger lock. The CLI takes `--assert-algorithm` and the config module's `assert_algorithm`.
- `online=True` on `autogenerate_migrations()`, and with `models` on `up()`, `rehearse()`, `impact()`, and `preflight()`, splits a PostgreSQL diff into migration `<id>`, which changes only the catalog in one transaction, and `<id>_online`, which runs outside a transaction to build indexes concurrently, run backfills, and validate constraints, and runs again from its first statement after a failure. On MySQL and MariaDB it does what `assert_algorithm=True` does; the CLI takes `--online` and the config module's `online`, and `plan_migrations()` on either migrator returns the generated migrations as a list.
- `rehearse(lock_timeout=seconds)` on either migrator, and the config module's `rehearsal_lock_timeout`, fail a rehearsal whose statement waits longer than that for a lock.
- `impact()` and `preflight()` on either migrator take the diff options `up()` takes. On SQLite, `exact_counts=True` and `--exact-counts` count each table's rows and read its bytes from `dbstat` instead of estimating them.

#### Changed

- `index_must_be_concurrent()` passes `CREATE INDEX ... ON ONLY`, which builds no index and cannot take `CONCURRENTLY`, so a partitioned table can be indexed without blocking writes.
- Error messages name the engine, such as "The live preflight does not cover SQLite.", where they named the `Dialects` member.

#### Fixed

- The destructive labels and the `no_drops()`, `no_table_rewrite()`, `index_must_be_concurrent()`, and `no_lock_without_timeout()` guards read comments and whitespace as each engine does, so a statement such as `DROP/**/TABLE t` is labelled. `destructive_statements()` takes a `dialect`.
- A new table's foreign key to a column or unique index that the same diff adds to an existing table is added after them, where the generated migration used to fail.
- The PostgreSQL schema read recognizes an invalid index, such as one a failed `CREATE INDEX CONCURRENTLY` leaves, and a constraint that is not validated, and the diff builds the index again or emits `VALIDATE CONSTRAINT`. The partitions of a partitioned table no longer read as extra tables.

### 2.25.0

#### Added

- `record_scratch_rehearsal(results)` on either migrator writes every rehearsal row a passing `rehearse(scratch=True)` proved onto the real database, and `run_outcome(applied, run)` returns the rehearsal outcome recorded for a run.
- `Migrator.read_schema(models)` reads the schema once for `plan()` and `autogenerate()`, which take it as `snapshot`, and `sustained plan` reads the schema once instead of up to three times.
- The `--json` commands print one object with an `error` key when a run fails, and a failed `migrate` lists the migrations it applied.
- A SQL migration file honours `DELIMITER <token>` lines, so a MySQL trigger or procedure body loads from a file.
- `AsyncAdapter.session()` keeps a block's statements on one database session, and `AsyncAdapter.autocommit_scope()` runs a non-transactional migration with the driver's transaction control off. A custom adapter that opens a new session per statement has to override `session()`.
- `MigrationStatement.destructive`, `sustained.types.ColumnReference`, and type stubs that accept `raw()` columns in `where()`, `having()`, `select()`, `orderBy()`, and `groupBy()`.
- The schema read records more of the catalog, including each object's own spelling, schemas, collations, `ON UPDATE`, `AUTO_INCREMENT`, and SQLite's unnamed checks, triggers, and views.

#### Changed

- A column or table name string must be an identifier path, `*`, `table.*`, or a call on one column such as `COUNT(*)`, and every identifier in it is quoted, so `orderBy(request.args["sort"])` can no longer put SQL into the statement. Any other string raises `ValueError`; write it with `QueryBuilder.raw()`.
- `whereIn()`, `whereExists()`, `havingIn()`, `havingExists()`, and their NOT and OR forms raise `ValueError` for a string argument, and `in_()` and `not_in()` raise it for a `str` or `bytes` argument. A subquery written as SQL needs `QueryBuilder.raw()`.
- An INSERT, UPDATE, or DELETE raises `ValueError` when the builder has a clause the write does not render, such as `orderBy()`, `limit()`, a join, or a CTE.
- Every alias, CTE name included, is quoted through `quote_alias()`, and a dotted alias raises `ValueError`.
- `up()`, `down()`, `down_to()`, `baseline()`, `repair()`, and `record_rehearsal()` raise `ValueError` inside an open `transaction()` or `async_transaction()` block, since their commits also committed the caller's statements.
- `down()` raises `MigrationError` while a failed attempt is on record, a down step that fails without a rollback marks its row as failed, and `on_error` fires when `down()` fails.
- A tracking table read that fails for a reason other than a missing table raises the driver's error, where it read as a database with no history.
- The migration checksum counts splitting or joining statements as an edit, rows written by earlier releases still match, and `repair()` rewrites them in the current format.
- A narrowing type change, such as `numeric(18,6)` to `numeric(18,2)`, is labelled destructive, and the destructive scan matches more forms of column drops and DELETE. The diff refuses to remove a value from a MySQL enum column.
- The default dialect quotes identifiers in DDL, so a table or column named `order` no longer breaks CREATE TABLE or the SQLite rebuild. The generated SQL changes, so a rehearsal row recorded for a generated migration no longer matches.
- The asyncpg adapter reads `%%` in a statement as one `%` sign, as psycopg does.

#### Fixed

- The package imports on Python 3.9 to 3.11, where `up(models=...)`, `plan`, `drift`, and the async migrator raised `SyntaxError`.
- Async transactions: a DuckDB transaction stays on one session, an async rehearsal counts as an open transaction, a failed commit in `async_transaction()` rolls the block back, and `AsyncMigrator` runs a non-transactional migration with transaction control off. An async rehearsal on DuckDB or SQLite no longer applies its migrations for real.
- The migration lock scope rolls the session back before it unlocks, so a failed non-transactional migration on Postgres no longer blocks every other migrator. `baseline()` refuses a migration with a failed row, and `AsyncMigrator.status()`, `statuses()`, and `validate()` no longer create the tracking table.
- A cancelled `acquire()` or `release()` keeps the pool slot, and both pools wake a waiter when a slot frees.
- Query rendering: `to_sql()` doubles literal `%` signs on Postgres and MySQL, dates, timestamps, decimals, and bytes render as SQL literals, and a many-to-many `leftJoinRelated()` keeps rows with no link row. On MSSQL, `first()` uses `TOP 1`, a nested CTE moves into the top-level WITH clause, string literals take the `N` prefix, and `MOD()` renders as `%`.
- Schema diff: the SQLite rebuild quotes every name and keeps collations, checks, UNIQUE constraints, and triggers, enum CHECKs on SQLite and SQL Server are diffed, and foreign key targets and actions are read on MySQL, MariaDB, and SQL Server. Drops follow the tables that point at them, DuckDB and SQL Server lift indexes and defaults across a column change, DuckDB plans converge, and SQL Server reads column lengths and precision.
- The SQL file splitter keeps semicolons inside strings, quoted identifiers, comments, and dollar-quoted bodies, a rehearsal records its rows in one transaction, `script("down")` renders a generated migration from its tracking row, and a 200,000-row insert on SQLite takes half the time.

### 2.24.2

#### Fixed

- A second join through the same many-to-many relation names its link table `<alias>_<link table>`, so two aliased joins through one relation no longer render the same link table twice. A repeated join through a link table without an alias raises `ValueError`.
- Every query builder method name matches without regard to case or underscores, as the documentation described, so `where_ilike`, `WHERE_IN`, and `COUNT()` resolve.

### 2.24.1

#### Fixed

- A non-transactional migration on a sqlite3 connection opened with `connect(factory=...)` runs outside the implicit transaction, so the rebuild's foreign key pragmas take effect.
- A schema read on Presto or Trino no longer raises when two schemas have a table of the same name.
- A statement given the pool inside `async_transaction(pool)` runs on the block's adapter, where it ran outside the transaction, and a nested block opens a savepoint.
- `ConnectionPool.release()` rolls back every connection it takes back, and `AsyncConnectionPool.release()` keeps an adapter whose rollback raised when it answers `SELECT 1`, so a DuckDB connection no longer keeps an open transaction or loses an in-memory database.
- A rehearsal leaves a migration with `transactional=False` out of the run and reports `up_ok` as `None`, so a run that contains one can pass.
- The model registry keeps the newest class after a module reload, the async Postgres schema read survives a missing view, a MySQL or SQL Server column change restates the column's current default and comment, and the rehearsal keys compute each checksum once.

### 2.24.0

#### Added

- `Migration(transactional=False)`, or a `-- sustained: no transaction` comment in a SQL file, runs a migration outside a transaction, which `CREATE INDEX CONCURRENTLY` needs. A failure leaves the earlier statements applied; clean up and run `repair()`.
- `AsyncMigrator` gains `script()`, `plan()`, and `drift()`, and its `up()` and `rehearse()` take `models` and the diff options.
- `AsyncConnectionPool` opens adapters from an async factory up to `max_size` and works with `Model.bind_async()`. Statements run through `pool.scope()`.
- Guards receive each statement as a `MigrationStatement`, which names its migration and whether it runs in a transaction, and `no_lock_without_timeout()` scopes a `SET LOCAL lock_timeout` to its own migration.
- Check constraints are read on MySQL, MariaDB, SQL Server, and DuckDB, so a declared `Check` diffs there, and DuckDB enum types are read from `duckdb_types()`.
- `crossJoin()` accepts a table on its own.

#### Changed

- Every parameter after `allow_out_of_order` on `up()` of either migrator is keyword-only.
- `Migration` raises `ValueError` for a checksum given on SQL, statement-list, or ddl steps, which hash themselves. Run `repair()` if a stored row no longer matches.
- `down()` refuses a migration whose checksum no longer matches its tracking row unless given `allow_changed=True` or `sustained down --allow-changed`, and refuses a revert count below 1.
- A run that includes `DELETE FROM`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, or `DROP SCHEMA ... CASCADE` needs a passing rehearsal row, or `--unrehearsed`, before `migrate` applies it.
- `no_lock_without_timeout()` reads the run in order, so a `SET lock_timeout` written after an `ALTER TABLE` no longer excuses it.
- An undeclared check on MySQL, MariaDB, SQL Server, or DuckDB is a diff note instead of a refusal, and the Postgres, SQL Server, and DuckDB schema reads cover the connection's schema and the schemas the models declare.

#### Fixed

- Query rendering: a `Subquery` binds its values as parameters, nested expressions render for the query's dialect, identifiers escape their quote character, and `on()` validates its operator. Raw SQL no longer counts a `?` inside a string literal, and an offset with no limit runs on SQLite.
- Every cursor closes when its statement finishes, which stops pyodbc and MySQL "commands out of sync" errors; `close()` joins the `Cursor` protocol, so a test double needs it.
- Transactions and pools: `transaction()` belongs to the thread that opened it, a query given a pool inside `transaction(pool)` runs on the pinned connection, `ConnectionPool.release()` rolls back and drops a connection that fails a probe, and `async_transaction()` works on psycopg2. A rehearsal on DuckDB rolls back its DDL.
- Migrations: both migrators raise `MigrationError` when the advisory lock is refused, `script()`, `status()`, `pending()`, `validate()`, and `plan` no longer create the tracking table, and `down()` checks the whole revert window before it reverts anything. `up(target=A)` followed by `up(target=B)` no longer asks for a rehearsal it already proved.
- A result set that repeats a column name raises `AmbiguousColumns`, attribute access for a column the row does not have raises, and a string model reference resolves through the module that declares the relation.
- Schema diff: views stay out of the schema read, a MySQL or SQL Server column change keeps NOT NULL, the default, the identity, and the comment, new tables are created in dependency order, and a nullability drift no longer regenerates the type. Presto and Trino raise `DialectError` for a table rebuild, and a SQLite rebuild of a referenced table runs with foreign keys off.
- The destructive scan labels `DELETE FROM`, `DROP VIEW`, `DROP MATERIALIZED VIEW`, `DROP DATABASE`, and `DROP SCHEMA ... CASCADE`, the statement splitter handles a semicolon followed by a comment, and a misnamed migration file raises. `plan --json` includes a custom guard's verdicts.

### 2.23.1

#### Fixed

- Athena parameterized queries run, with `?` as the placeholder; set `pyathena.paramstyle = "qmark"` (pyathena 3 or later). Parameters are sent as strings through the new `prepare_execution` hook, which `to_sql()` output needs when you execute it yourself.
- Athena DDL quotes identifiers with backticks, every string column renders `STRING` for Iceberg tables without drifting, and introspection reads only the connection's schema.

### 2.23.0

#### Added

- Every column definition takes a `comment`, which introspection reads back and the diff compares, and `set_column_comment` sets one in a hand-written migration. Athena refuses a change after `CREATE TABLE`.

### 2.22.0

#### Added

- `Binary()` in `sustained.schema` declares a bytes column in each dialect's type, such as `BYTEA` on Postgres and `VARBINARY(MAX)` on SQL Server.
- The **Covered** column on the [support page](https://sustained.tbmh.org/support) maps each claim to a module in `tests/integration`, and a contract test fails when they disagree.
- `matrix.py` gains a `<name>-latest` target per container database, which runs the newest release the vendor supports.

#### Fixed

- `transaction()` nests with `SAVE TRANSACTION` on SQL Server and raises `DialectError` for nesting on DuckDB, and it works on the duckdb driver, so a failed multi-statement migration rolls back. sqlite3 connections in legacy transaction control get an explicit `BEGIN`.
- A NOT NULL change with a `backfill` runs on DuckDB, plain indexes are read back on MySQL, MariaDB, SQL Server, and DuckDB, and `union()` and the other set operations run on SQLite.

### 2.21.0

#### Added

- `Enum(*values, name=...)` in `sustained.schema` declares a column over a named, ordered value list, and migration generation adds values to it. Removing or reordering values refuses with a rebuild recipe.
- `Check(name, expression)` and `ForeignKey(name, columns, references, on_delete=, on_update=)` declare named table constraints in `tableConstraints`, and migration generation adds them, with changed and undeclared ones gated by `allow_drops`.
- `sustained.ddl` provides typed steps for hand-written migrations that render for the dialect at run time, so one migration serves every dialect. A migration whose up step is all reversible ddl steps derives its down step.
- The Postgres schema read covers foreign key targets, non-unique indexes, varchar lengths, precision and scale, CHECK expressions, and enum values.

#### Changed

- `DROP TYPE` and `DROP CONSTRAINT` count as destructive: `plan` labels them, `no_drops()` blocks them, and they need a rehearsal.
- Declared table constraints raise `DialectError` on Presto and Athena.

### 2.20.0

#### Changed

- The row a rehearsal writes is called a rehearsal row, where it was a receipt: `rehearsal_key()` replaces `receipt_key()`, and the outcome constants are `REHEARSAL_PASSED`, `REHEARSAL_FAILED`, and `REHEARSAL_OVERRIDE`. Stored rows are untouched, so a rehearsal recorded by an earlier version still opens the gate.
- `sustained rehearse` prints `rehearsal row recorded` and `rehearsal row not recorded`, where it printed `receipt`.
- `receipt_key()`, `RECEIPT_PASSED`, `RECEIPT_FAILED`, and `RECEIPT_OVERRIDE` are deprecated. Each raises a `DeprecationWarning` naming its replacement and goes away in 3.0.

### 2.19.0

#### Added

- A written [support policy](https://sustained.tbmh.org/support) that lists the supported databases and Python versions and states the deprecation path and what each version number promises. `sync_support.py` renders it from `support.json`.
- An integration suite in `tests/integration/` that runs the migration lifecycle and a query on every supported server, and `matrix.py`, which starts the servers from `docker/compose.yaml` and prints one line per server.
- `Compiler.compile_create_table()` renders the whole CREATE TABLE statement.

#### Fixed

- The tracking table works on MySQL, where the `generated` column name is reserved, and on SQL Server, which has no `CREATE TABLE IF NOT EXISTS`. The MySQL advisory lock is taken on MariaDB.

### 2.18.0

#### Added

- `Dialects.MYSQL` compiles for MySQL and MariaDB, with upserts, `AUTO_INCREMENT`, a `GET_LOCK` advisory lock, and `for_update()` with `SKIP LOCKED` and `NOWAIT` on MySQL 8.0. `rehearse()` needs a scratch database there, and `returning()`, `STRING_AGG`, and a unique key or literal default on a `Text()` or `Json()` column raise.
- Column types render in the spelling `information_schema` reports back, so tables created from models do not drift on MySQL, and MariaDB `Json()` columns read back as JSON.
- `Compiler.supports_transactional_ddl()` reports whether rolled-back DDL goes away, and `Compiler.inline_references()` reports whether an inline `REFERENCES` clause creates a foreign key.

#### Changed

- Schema reading moved from `sustained.autogenerate` to `sustained.introspect`, and every name still imports from the old module.
- Default and type normalization treat `current_timestamp()` and `CURRENT_TIMESTAMP`, and the `*TEXT` variants, as equal.

### 2.17.0

#### Added

- The tracking table stores a generated migration's up and down statements, so `down()` can revert a migration the process never diffed.
- `up(unrehearsed=True)` records the waiver as a rehearsal row with the outcome `override`, which never opens the gate for a later run.
- `migrate` exits 4 when a run that removes data has no passing rehearsal.
- `Migrator.drift()` and `SchemaDiff.outstanding()` take `ignore_changed_columns`.

#### Fixed

- A SQLite table rebuild keeps undeclared columns, their data, and hand-made indexes, and an index on an expression no longer crashes introspection.
- Rehearsals with models and rename hints run, blame only the down steps that failed, apply the generated migration before the repeatables, and record rows that cover `migrate --target`.
- `no_lock_without_timeout()` is Postgres only and fires only on a `SET` statement, and the `plan` footer no longer says `run: sustained rehearse` after a rehearsal recorded its row.
- Filter and write values accept `datetime`, `date`, `Decimal`, and `bytes` under a strict type checker, and a failed savepoint no longer reuses a savepoint name.

### 2.16.1

#### Added

- `Connection`, `Cursor`, `Binding`, `SqlValue`, `RowValue`, `ColumnDescription`, and `RelationTree` in `sustained.types`, and driver protocols in `sustained.aio`, so any DB-API 2.0 driver connection type-checks.

#### Changed

- Roughly 180 `Any` annotations were replaced with specific types, and migration callbacks and callable steps are typed.
- `in_()` and `not_in()` accept any sequence of values.

### 2.16.0

#### Added

- The query builder is generic over its model: `Show.query()` is a `QueryBuilder[Show]`, so `run()` is `List[Show]` and `first()` is `Optional[Show]`. The select list does not narrow the result type.
- `WriteBuilder[Model]` types the result of `insert()`, `update()`, `delete()`, and the other writes, as the row count or the RETURNING rows.

#### Changed

- Argument positions that take any query are declared `QueryBuilder[Any]`.

### 2.15.0

#### Added

- Guards, rules over the statements a run would apply that return `block` or `warn` verdicts. Both migrators and the CLI config module take `guards=[...]`, and `up()` raises `GuardBlocked` before any statement runs.
- `sustained.guards` provides `no_drops()`, `index_must_be_concurrent()`, `no_table_rewrite()`, `no_lock_without_timeout()`, and `max_statements(n)`.
- `sustained plan` prints a `guards` section, and `plan` and `migrate` exit 3 when a guard blocks a statement.
- Both migrators take `callbacks=Callbacks(...)`, so `before_migrate`, `after_migrate`, and `on_error` reach library callers.

#### Changed

- `after_migrate` fires before the post-run drift report, and `rehearse` does not enforce guards.

### 2.14.0

#### Added

- A passing rehearsal writes a rehearsal row, and `up()` refuses a run that removes data, such as a DROP TABLE, a column drop, or a TRUNCATE, unless a passing rehearsal covers that exact run. `up(unrehearsed=True)` and `sustained migrate --unrehearsed` apply anyway, and the refusal raises `RehearsalRequired`.
- `record_rehearsal()`, `rehearsal_outcome()`, and `rehearsed()` on both migrators, and a `rehearsal_table` option on both constructors and the CLI config module.

#### Changed

- `rehearse()` returns a `Rehearsal`, a `list` that also has `key`, `recorded`, and `ok`, and `rehearse(scratch=True)` records nothing through the API.
- `sustained plan` prints `run: sustained rehearse` when a pending migration removes data, and Sustained's own tables are left out of every diff.

### 2.13.0

#### Added

- `Migrator.up(models=[...])` diffs the models against the database and applies the generated migration after everything else pending, and `sustained migrate` and `sustained rehearse` pass the config module's `models`.
- `Migrator.rehearse(models=[...])` rehearses the generated migration and reports whether the models landed and the down steps reversed. `sustained rehearse --json` prints the result.
- `Migrator.drift(models)` returns what the models still ask for, and `sustained migrate` reports it after a run.

#### Changed

- `plan --json` reports each pending migration's statements with a destructive flag, and `PendingSummary.statements` became `PendingSummary.sql`.
- The generated diff no longer refuses objects the models do not declare; `ignore_undeclared=False` restores the refusal.
- `Migrator.sync()` is deprecated in favour of `up(models=[...])` and goes away in 3.0.

### 2.12.0

#### Added

- `withGraphFetched()` takes a dotted path such as `'shows.tickets'` and loads each level in one batched query.
- Async eager loading covers link-table relations and dotted paths, and `async_transaction()` nests through savepoints.

#### Fixed

- The type stubs describe the join and clause methods the runtime accepts, `LENGTH` keeps its registration, and `IntrospectedTable` and `FunctionMetadata` instances no longer share mappings.

### 2.11.0

#### Changed

- A targeted `up()` does not run the repeatables; the next full `up()` runs them.
- The destructive scan labels a column drop written without the COLUMN keyword.

#### Fixed

- `repair()` keeps the stored checksum of a changed repeatable, so the change still re-runs it.
- A malformed placeholder such as `${my-key}` raises `ValueError` naming the file.
- `rehearse()` reads the migration state inside the advisory lock, and `AsyncMigrator.rehearse()` refuses a connection in autocommit mode.

### 2.10.0

#### Added

- `sustained rehearse` and `Migrator.rehearse()` apply every pending migration, run the down steps, and roll it all back, on SQLite, Postgres, and DuckDB. A config module's `get_rehearsal_connection()` sends the rehearsal to a scratch database.
- Config module callbacks `before_migrate`, `after_migrate`, and `on_error` around `sustained migrate`.
- `Migrator.connection` and `AsyncMigrator.adapter` properties.

#### Changed

- A failing statement, a connection that will not open, or a directory that will not load prints as an error line on the command line instead of a traceback.

### 2.9.0

#### Added

- `sustained plan` shows the pending migrations, the problems `validate` would report, and the drift against the config module's `models`, and exits 0 when current, 2 when pending, and 1 on problems.
- `sustained.analysis` labels drops and truncates in the plan as destructive.
- `--json` on `status`, `validate`, and `plan` prints one JSON object.

### 2.8.0

#### Added

- Repeatable migrations, a `<id>.repeat.sql` file or `Migration(id, up, repeatable=True)`, run again whenever their checksum changes.
- `statuses()` on both migrators returns each migration's state, which `sustained status` prints.
- `${key}` placeholders in SQL migration files, filled from `load_migrations(placeholders=...)` or the config module.

#### Changed

- `pending()` also returns repeatables whose checksum changed.

### 2.7.0

#### Added

- `load_migrations(directory)` loads migrations from `<id>.up.sql` and `<id>.down.sql` files.
- `baseline(target)` on both migrators records migrations up to the target as applied without running them.
- `Migrator.plan(models, ...)` returns the migration `sync()` would generate without applying it.
- The `sustained` command and `python -m sustained` drive a `Migrator` from a config module, with `status`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline`.

### 2.6.0

#### Added

- The tracking table records a sequence number, a checksum, the execution time, and a success flag, and older tables upgrade in place.
- `validate()` on both migrators raises `MigrationError` on failed attempts, unknown applied ids, checksum mismatches, and out-of-order pending migrations, and `repair()` removes failed attempts and rewrites checksums.
- Migration runs take an exclusive advisory lock on Postgres and MSSQL.

#### Changed

- `up()` validates before running. `validate=False` skips the checks, and `allow_out_of_order=True` accepts a pending migration ordered before an applied one.

#### Fixed

- The tracking table upgrade keeps a recorded failed attempt after an interrupted earlier upgrade.

### 2.5.0

#### Added

- The AWS Athena dialect, `Dialects.ATHENA`, with MERGE upserts on Iceberg tables and Athena's type spellings.
- `TableOptions(location, partitioned_by, properties)` declares a model's storage clauses on Athena.
- Migrations on engines without transactions, and a tracking table created without constraints where the engine has none.

### 2.4.0

#### Added

- Introspection reads primary keys, unique constraints, foreign keys, column defaults, and indexes.
- Type and nullability changes generate migrations, with a table rebuild on SQLite, and `renames` and `table_renames` hints generate renames instead of a drop and an add.
- Declared indexes via `Index`, and a `backfill` on a column definition for NOT NULL adds.
- `migration_sql()` and `Migrator.script()` render the SQL a run would execute.
- `AsyncMigrator` runs migrations on an `AsyncAdapter`.

### 2.3.0

#### Added

- `diff_schema()` compares the models with the live database, and `autogenerate()` builds a `Migration` from the difference.
- `Migrator.sync(models)` diffs, generates, and applies in one call, and `Migrator.down_to(id)` reverts to a target.

### 2.2.0

#### Added

- Typed column definitions in `tableColumns`, and `create_table_sql()`, `create_table()`, and `drop_table()` on models.
- A migration runner with ordered `Migration` objects, up and down steps, a tracking table, and targets.
- `ConnectionPool`, a thread-safe pool of DB-API connections that `Model.bind()` and every execution entry point accept.
- Async execution with `arun()`, `afirst()`, and `ato_dicts()` through `DbApiAsyncAdapter`, `AiosqliteAdapter`, and `AsyncpgAdapter`, with `Model.bind_async()` and `async_transaction()`.

### 2.1.0

#### Added

- Typed predicates such as `Model.c.age > 21` and `col()`, combined with `&`, `|`, and `~`, and `whereRaw()` and `havingRaw()` with `?` value markers.
- `Model.transaction()` with savepoint nesting, and `set_statement_listener()` for the SQL, parameters, and duration of every statement.
- Upserts with `insert().onConflict(cols).merge()` and `.ignore()`, `insert_from()`, and `create_table_as()`.
- `to_dicts()`, `to_df()`, and `to_arrow()` result formats.
- The DuckDB dialect.
- Recursive CTEs, `intersect()` and `except_()`, `distinctOn()`, `groupByRollup()`, `groupByCube()`, `groupByGroupingSets()`, `qualify()`, `for_update()`, `total()`, `cursor_page()`, and `explain()`.
- Eager loading of `ManyToManyRelation`, and per-dialect function names such as `NOW()` as `GETDATE()` on MSSQL.

### 2.0.0

#### Added

- `to_sql()` returns the statement and its parameters, and `insert()`, `update()`, `delete()`, and `returning()` build writes.
- `Model.bind(connection)`, `run()`, and `first()` run queries on any DB-API 2.0 connection and return model instances.
- `withGraphFetched()` eager loading for HasMany, HasOne, and BelongsToOne relations.
- Class-level column access such as `User.id`, an optional `columns` declaration, and a model registry for string `modelClass` references.
- `clone()`, `page()`, snake_case aliases for every camelCase method, window function arguments and frames, the `'column AS alias'` shorthand, and `IS NULL` for a comparison with `None`.

#### Changed

- **Breaking:** String arguments to `select_func()` and the dynamic function methods are column references; wrap literal values in `Literal()`.
- **Breaking:** `where()` and `having()` validate operators against an allowlist, `update()` and `delete()` require a `where()` clause, and an empty `whereIn()` list or two CTEs with one alias and different definitions raise `ValueError`.
- **Breaking:** `top()` raises `DialectError` outside MSSQL, and on MSSQL `limit()` and `offset()` raise it without an `ORDER BY`.
- **Breaking:** `whereILike()` compiles to `LOWER(col) LIKE LOWER(pattern)` without native ILIKE, booleans render as `TRUE` and `FALSE` (`1` and `0` on MSSQL), and column references in WHERE, HAVING, and GROUP BY are quoted per dialect.
- **Breaking:** `with_()` requires a `QueryBuilder` and renders it when the query renders.
- **Breaking:** The minimum Python version is 3.9.

#### Fixed

- `copy.copy`, `copy.deepcopy`, and `pickle` work on builders and models.
- Union members keep their `ORDER BY` and `LIMIT`, nested CTEs move into the top-level `WITH` clause, `GROUP BY` and MSSQL quote a dotted path as a path, and Presto renders `OFFSET` before `LIMIT`.
- `limit()`, `offset()`, and `top()` reject booleans and negative numbers.

## 1.x

### 1.1.0

#### Added

- A query builds once and compiles for a chosen dialect: the default, PostgreSQL, MSSQL, or Presto.
- `select_func()` and the function methods raise `DialectError` at build time for a function the dialect does not support.

### 1.0.2

#### Fixed

- The type stubs declare the join methods they were missing.

### 1.0.1

#### Fixed

- Installs include the type stubs.

### 1.0.0

#### Added

- Type stub files for the builders, packaged with the distribution.

## 0.x

### 0.0.7

#### Added

- `USING` clauses on joins, and subqueries in JOIN ON clauses.
- LIKE and NULL checks in WHERE and HAVING clauses.

### 0.0.6

#### Added

- `distinct()` on the query builder.
- `avg()`, `min()`, and `max()` aggregate methods.
- `Func`, for calling any SQL function, and `Subquery`, for embedding a subquery in the SELECT list.

### 0.0.5

#### Added

- A select clause builder with fluent methods for complex select lists.
- `Column`, for marking a value as a column reference rather than a literal.
- The expression classes export from the top-level package.

#### Changed

- `Any` annotations across the codebase were replaced with specific types.

### 0.0.4

#### Added

- ORDER BY, LIMIT, TOP, and OFFSET clauses.
- UNION queries.
- Subqueries in FROM expressions and in conditional clauses, and EXISTS and BETWEEN conditions.

### 0.0.3

First tagged release. SELECT query building with joins, WHERE, GROUP BY, and HAVING clauses, relation-aware joins through `joinRelated()`, and a builder split into per-clause components.

#### Fixed

- `andWhere()` could start a WHERE clause on its own.
