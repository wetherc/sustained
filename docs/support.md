---
layout: default
title: Support Policy
---

This page describes supported databases, database versions, and Python versions, and provides a guaranteed deprecation and removal policy.

## What support means

For every database with a server in the table, "supported" means that we guarantee Sustained's behavior against this database for all versions of the database between the minimum and maximum listed versions (inclusive). Our testing posture currently only runs a full suite of integration tests against the oldest supported and newest supported versions, and assumes any intermediate version between those remains compatible with Sustained. This is, notably, a gap in our posture today and one that will be closed; however, it is called out here until then.

Note that the ANSI dialect has no server to run. Sustained compiles SQL for it, and unit tests check the SQL text.

<!-- databases: generated from support.json -->

| Database | Versions | Where it runs | Covered | Notes |
| --- | --- | --- | --- | --- |
| PostgreSQL | 12 and later; suite runs 14 and 18 | Container (postgres:14-alpine, postgres:18-alpine) | queries, writes, transactions, migrations, async | Rehearsal, advisory locks, and transactional DDL all run here. Enum ADD VALUE inside a transaction sets the floor. |
| MySQL | 8.0.19 and later; suite runs 8.4 and 26.7 | Container (mysql:8.4, mysql:26.7) | queries, writes, transactions, migrations | No transactional DDL, so rehearse needs scratch=True. ALTER TABLE DROP CONSTRAINT sets the floor. |
| MariaDB | 10.6 and later; suite runs 11.4 and 12.3 | Container (mariadb:11.4, mariadb:12.3) | queries, writes, transactions, migrations | Same dialect as MySQL. SKIP LOCKED sets the floor. JSON columns read back through their json_valid check. |
| SQL Server | 2012 and later; suite runs 2022 and 2025 | Container (mcr.microsoft.com/mssql/server:2022-latest, mcr.microsoft.com/mssql/server:2025-latest) | queries, writes, transactions, migrations | Off the rehearsal allowlist, so rehearse refuses without scratch=True. OFFSET with FETCH sets the floor. pyodbc needs the ODBC driver installed. |
| Presto and Trino | 351 and later; suite runs 468 and 483 | Container (trinodb/trino:468, trinodb/trino:483) | queries | Reads the tpch catalog. Neither server takes the migration surface. The floor is the first release under the Trino name; PrestoDB works for the surface its engine has, untested here. |
| SQLite | 3.35 and later | In process | queries, writes, transactions, migrations, async | Standard library. The table rebuild path lives here. RETURNING and DROP COLUMN set the floor; sqlite3.sqlite_version says what your Python links. |
| DuckDB | 1.0 and later | In process | queries, writes, transactions, migrations | In-process, so no container and no advisory lock. Releases before 1.0 are not claimed. |
| AWS Athena | engine version 3 and later | Your AWS account | queries | Your AWS account, with a staging S3 directory. Iceberg MERGE sets the floor at engine version 3. Athena's migration surface is not exercised, since the tables it needs live in your buckets. |
| ANSI (default) | Any | Nothing to run | SQL text only | The portable compiler. Any DB-API 2.0 driver that takes this SQL will work, untested here. |

<!-- end databases -->

The **Covered** column says what Sustained features are integration tested for that database. `queries` includes building and executing SELECT, INSERT, UPDATE, and DELETE. `migrations` is the migration lifecycle: `migrate`, `rehearse`, `down`, `validate`, and `repair`, plus reading the schema back. Databases that don't support transactions (Presto) are not tested against migrations. SQL dialects that have no execution engine (ANSI) only evaluate the syntactic correctness of generated SQL.

## Database versions

The **Versions** column lists a floor and a suite version. The floor is the oldest release of that server on which every statement Sustained generates is valid. It is a property of the compiler: each dialect's floor is set by the newest SQL construct it emits, and the row's note names that construct. On a release older than the floor, the statements that use that construct fail in the server; everything else continues to work. The floor version will only ever be updated in a major release of Sustained.

The suite version is the database version that the integration tests run against and is the oldest release the vendor still supports. When the vendor ends support for it, the suite version moves to the next release in a minor release of Sustained, and is noted in the changelog.

All database releases newer than the suite version are expected to work.

To check your exact version, point the suite at your server and run it:

```console
$ SUSTAINED_TEST_POSTGRES_DSN=postgresql://user:pass@host/db python3 matrix.py postgres
ran     postgres    10 tests, queries, migrations
```

A clean run verifies your exact release the same way the suite verifies the versions in the table. [Run it yourself](#run-it-yourself) describes the runner.

SQLite has no server: the version tested against is the one your Python links. You can find this with `sqlite3.sqlite_version`.

## Python versions

<!-- python: generated from support.json -->

Sustained runs on CPython 3.9 and later. The test suite runs on 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14.

<!-- end python -->

A Python version stays supported until CPython itself ends support for it. After that, it is dropped in a minor release. The release before support is removed will always list it as an upcoming change in the CHANGELOG.

## Run it yourself

Running `matrix.py` in the repository starts each database server evaluated and runs the integration suite against it:

```console
$ python3 matrix.py
starting postgres, mysql, mariadb, mssql, presto
ran     postgres    10 tests, queries, migrations
ran     mysql       10 tests, queries, migrations
ran     mariadb     10 tests, queries, migrations
ran     mssql       10 tests, queries, migrations
ran     presto      2 tests, queries
ran     sqlite      10 tests, queries, migrations
ran     duckdb      10 tests, queries, migrations
waiting athena      SUSTAINED_TEST_ATHENA_S3_DIR is not set
removing postgres, mysql, mariadb, mssql, presto

1 of 8 still waiting
```

Containers are defined in `docker/compose.yaml` and are removed at the end of the test suite. Docker is obviously a prerequisite, although the test suite doesn't require you interact with it directly. You can name a target database engine to run only tests against that database:

```console
$ python3 matrix.py postgres
$ python3 matrix.py python
$ python3 matrix.py --check
```

Aa skipped test is reported as a failure. Exit codes are 0 for a clean run, 1 for a failure, and 2 when nothing failed and something was still waiting.

To test against an existing server, set its connection variable, for example `SUSTAINED_TEST_POSTGRES_DSN`, and the test runner will not start a separate container for it. Athena runs in your own AWS account: point `SUSTAINED_TEST_ATHENA_S3_DIR` at a staging directory and supply a profile with `--athena-profile`.

## Deprecation

A public name is anything the documentation names: a class, a function, a method, a keyword argument, a CLI command, a CLI flag, or an exit code.

A public name is removed in three steps.

1. **Warn.** The name keeps working and raises a `DeprecationWarning` that names its replacement. The changelog entry says the same thing.
2. **Wait.** At least one minor release ships with the warning in place.
3. **Remove.** The name is removed in the next major release, and only there.

For example, `sync()` was replaced by `up(models=[...])` in v2.13.0. It will still run, emitting a `DeprecationWarning`, until it is removed in v3.0.

If support for a database engine is ever removed, that will follow this same policy.

## What a version number promises

- A **patch** release fixes a defect. Working code keeps working, and the SQL that comes out is the same, unless the SQL was the defect.
- A **minor** release adds behaviour, deprecates a name, drops an unsupported Python version, or moves a server version forward. Working code keeps working, and warnings may be new.
- A **major** release removes deprecated names and may change behaviour that working code depends on. The changelog lists every removal.

Generated SQL is part of the promise. A statement that changes between patch releases is a defect.

## Security problems

Report a security problem through a private advisory on the [GitHub repository](https://github.com/wetherc/sustained/security/advisories), not a public issue. Fixes ship in a patch release against the newest minor version. Older minor versions are not patched.
