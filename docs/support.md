---
layout: default
title: Support Policy
---

Sustained supports a fixed list of databases, database versions, and Python versions, and a deprecation policy governs how any of them is removed.

## What support means

For every database in the table that has a server, "supported" means that we guarantee Sustained's behavior against every version of that database from the floor to the newest suite version, inclusive. We currently run the full integration suite only against the two suite versions, and we assume that every other version in that range remains compatible with Sustained. We plan to close this gap in our testing.

The ANSI dialect has no server to run, so Sustained compiles SQL for it and unit tests check the SQL text.

<!-- databases: generated from support.json -->

| Database | Versions | Where it runs | Covered | Notes |
| --- | --- | --- | --- | --- |
| PostgreSQL | 12 and later; suite runs 14 and 18 | Container (postgres:14-alpine, postgres:18-alpine) | queries, writes, transactions, migrations, async | Rehearsal, advisory locks, and transactional DDL all run here. Enum ADD VALUE inside a transaction sets the floor. |
| MySQL | 8.0.19 and later; suite runs 8.4 and 26.7 | Container (mysql:8.4, mysql:26.7) | queries, writes, transactions, migrations | No transactional DDL, so rehearse needs scratch=True. ALTER TABLE DROP CONSTRAINT sets the floor. |
| MariaDB | 10.6 and later; suite runs 11.4 and 12.3 | Container (mariadb:11.4, mariadb:12.3) | queries, writes, transactions, migrations | Same dialect as MySQL. SKIP LOCKED sets the floor. JSON columns read back through their json_valid check. |
| SQL Server | 2012 and later; suite runs 2022 and 2025 | Container (mcr.microsoft.com/mssql/server:2022-latest, mcr.microsoft.com/mssql/server:2025-latest) | queries, writes, transactions, migrations | Off the rehearsal allowlist, so rehearse refuses without scratch=True. OFFSET with FETCH sets the floor. pyodbc needs the ODBC driver installed. |
| Presto and Trino | 351 and later; suite runs 468 and 483 | Container (trinodb/trino:468, trinodb/trino:483) | queries | Reads the tpch catalog. Neither server runs migrations. The floor is the first release under the Trino name; PrestoDB works for the features its engine has, untested here. |
| SQLite | 3.35 and later | In process | queries, writes, transactions, migrations, async | Standard library. The table rebuild path lives here. RETURNING and DROP COLUMN set the floor; sqlite3.sqlite_version says what your Python links. |
| DuckDB | 1.0 and later | In process | queries, writes, transactions, migrations | In-process, so no container and no advisory lock. Releases before 1.0 are not claimed. |
| AWS Athena | engine version 3 and later | Your AWS account | queries | Your AWS account, with a staging S3 directory. Iceberg MERGE sets the floor at engine version 3. Migrations are not tested on Athena, since the tables they need live in your buckets. |
| ANSI (default) | Any | Nothing to run | SQL text only | The portable compiler. Any DB-API 2.0 driver that takes this SQL will work, untested here. |

<!-- end databases -->

The **Covered** column names the feature sets the integration suite runs against that database. Each name maps to one test module in `tests/integration`:

- `queries` is every read feature: joins, eager loading, aggregates, window functions, CTEs, set operations, subqueries, `LIMIT` and `OFFSET`, and hydration to models, dicts, DataFrames, and Arrow tables.
- `writes` is `INSERT`, `UPDATE`, `DELETE`, upserts through `onConflict()`, `RETURNING`, `INSERT ... SELECT`, and `CREATE TABLE AS`.
- `transactions` is commit and rollback as observed from a second connection, savepoint nesting, and `ConnectionPool`.
- `migrations` is the migration lifecycle: `migrate`, `rehearse`, `down`, `validate`, and `repair`, plus schema introspection, column type and column comment round trips, and SQL file migrations.
- `async` is `arun()`, `async_transaction()`, and `AsyncMigrator` on an async driver.

Where a dialect does not implement a feature (for example `RETURNING` on MySQL), we test that `to_sql()` raises `DialectError` and that nothing reaches the server.

We do not test writes or migrations against databases that do not support transactions (Presto). For SQL dialects that have no execution engine (ANSI), the tests check only that the generated SQL is syntactically correct.

## Database versions

The **Versions** column lists a floor and, for databases that run in a container, two suite versions. Each dialect's floor is the oldest database version that can execute Sustained's full set of SQL statements, and the Notes column lists the statements that version added. On a release older than the floor, only those unsupported statements fail and everything else continues to work. We change the floor only in a major release of Sustained.

The first suite version is the oldest release the vendor still supports. When the vendor ends support for that release, a minor release of Sustained moves the suite version to the next release and notes the change in the changelog. The second suite version is the vendor's latest release, and it sets the upper end of the supported range.

To check your exact version, point the suite at your server and run it:

```console
$ SUSTAINED_TEST_POSTGRES_DSN=postgresql://user:pass@host/db python3 matrix.py postgres
ran     postgres         59 tests, queries, writes, transactions, migrations, async
```

A clean run verifies your exact release the same way the suite verifies the versions in the table. [Run it yourself](#run-it-yourself) describes the runner.

SQLite has no server, so the version tested against is the one your Python links, which `sqlite3.sqlite_version` reports.

## Python versions

<!-- python: generated from support.json -->

Sustained runs on CPython 3.9 and later. The test suite runs on 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14.

<!-- end python -->

A Python version stays supported until CPython itself ends support for it, and after that we drop it in a minor release. The release before the one that removes support always lists the removal as an upcoming change in the changelog.

## Run it yourself

Running `matrix.py` in the repository starts each database server and runs the integration suite against it:

```console
$ python3 matrix.py
starting postgres, postgres-latest, mysql, mysql-latest, mariadb, mariadb-latest, mssql, mssql-latest, presto, presto-latest
ran     postgres         59 tests, queries, writes, transactions, migrations, async
ran     postgres-latest  59 tests, queries, writes, transactions, migrations, async
ran     mysql            53 tests, queries, writes, transactions, migrations
ran     mysql-latest     53 tests, queries, writes, transactions, migrations
ran     mariadb          53 tests, queries, writes, transactions, migrations
ran     mariadb-latest   53 tests, queries, writes, transactions, migrations
ran     mssql            53 tests, queries, writes, transactions, migrations
ran     mssql-latest     53 tests, queries, writes, transactions, migrations
ran     presto           19 tests, queries
ran     presto-latest    19 tests, queries
ran     sqlite           59 tests, queries, writes, transactions, migrations, async
ran     duckdb           53 tests, queries, writes, transactions, migrations
waiting athena           SUSTAINED_TEST_ATHENA_S3_DIR is not set
removing postgres, postgres-latest, mysql, mysql-latest, mariadb, mariadb-latest, mssql, mssql-latest, presto, presto-latest

1 of 13 still waiting
```

`docker/compose.yaml` defines the containers, and the runner removes them when the suite ends. You need Docker installed, but you never interact with it directly. To run the tests against only one database engine, name it as the target:

```console
$ python3 matrix.py postgres
$ python3 matrix.py python
$ python3 matrix.py --check
```

Each container database also has a `<name>-latest` target, for example `postgres-latest`, which runs the same tests against the newest release the vendor supports.

The runner reports a skipped test as a failure. Exit codes are 0 for a clean run, 1 for a failure, and 2 when nothing failed and something was still waiting.

To test against an existing server, set its connection variable, for example `SUSTAINED_TEST_POSTGRES_DSN`, and the test runner will not start a separate container for it. Athena runs in your own AWS account: point `SUSTAINED_TEST_ATHENA_S3_DIR` at a staging directory and supply a profile with `--athena-profile`.

## Deprecation

A public name is anything the documentation names: a class, a function, a method, a keyword argument, a CLI command, a CLI flag, or an exit code.

To remove a public name, we follow these steps:

1. **Warn.** The name keeps working and raises a `DeprecationWarning` that names its replacement. The changelog entry says the same thing.
2. **Wait.** At least one minor release ships with the warning in place.
3. **Remove.** The name is removed in the next major release, and only there.

For example, v2.13.0 deprecated `sync()` in favor of `up(models=[...])`. `sync()` still runs and emits a `DeprecationWarning` until v3.0 removes it.

If we ever remove support for a database engine, the removal follows the same policy.

## What a version number promises

- A **patch** release fixes a defect. Working code keeps working, and the SQL that comes out is the same, unless the SQL was the defect.
- A **minor** release adds behaviour, deprecates a name, drops an unsupported Python version, or moves a server version forward. Working code keeps working, and warnings may be new.
- A **major** release removes deprecated names and may change behaviour that working code depends on. The changelog lists every removal.

Generated SQL is part of this promise, so a statement that changes between patch releases is a defect.

## Security problems

Report a security problem through a private advisory on the [GitHub repository](https://github.com/wetherc/sustained/security/advisories), not a public issue. Security fixes ship in a patch release against the newest minor version, and we do not patch older minor versions.
