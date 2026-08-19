---
layout: default
title: Support Policy
---

This page says which databases and Python versions Sustained supports, what
support means, and how much warning you get before something goes away. The
tables come from `support.json` in the repository, and a pre-commit hook
fails when this page and that file disagree. The claim on this page and the
list the test suite reads are the same list.

## What support means

For every database with a server in the table, the integration suite applies
migrations to a real server of that database and reads the schema back
afterwards. A container starts, the tests run against it, and the container
stops. SQLite and DuckDB run in process, and Athena runs in your own AWS
account, but the suite is the same suite.

The ANSI dialect has no server to run. Sustained compiles SQL for it, and
unit tests check the SQL text. Nothing executes. The SQL is correct as far
as the compiler is concerned, and your driver is where you find out the rest.

Nothing sits between the two. A database Sustained cannot run against is
never described as partly supported.

<!-- databases: generated from support.json -->

| Database | Versions | Where it runs | Covered | Notes |
| --- | --- | --- | --- | --- |
| PostgreSQL | 12 and later; suite runs 14 | Container (postgres:14-alpine) | queries, writes, transactions, migrations | Rehearsal, advisory locks, and transactional DDL all run here. Enum ADD VALUE inside a transaction sets the floor. |
| MySQL | 8.0.19 and later; suite runs 8.4 | Container (mysql:8.4) | queries, writes, transactions, migrations | No transactional DDL, so rehearse needs scratch=True. ALTER TABLE DROP CONSTRAINT sets the floor. |
| MariaDB | 10.6 and later; suite runs 11.4 | Container (mariadb:11.4) | queries, writes, transactions, migrations | Same dialect as MySQL. SKIP LOCKED sets the floor. JSON columns read back through their json_valid check. |
| SQL Server | 2012 and later; suite runs 2022 | Container (mcr.microsoft.com/mssql/server:2022-latest) | queries, writes, transactions, migrations | Off the rehearsal allowlist, so rehearse refuses without scratch=True. OFFSET with FETCH sets the floor. pyodbc needs the ODBC driver installed. |
| Presto and Trino | 351 and later; suite runs 468 | Container (trinodb/trino:468) | queries | Reads the tpch catalog. Neither server takes the migration surface. The floor is the first release under the Trino name; PrestoDB works for the surface its engine has, untested here. |
| SQLite | 3.35 and later | In process | queries, writes, transactions, migrations | Standard library. The table rebuild path lives here. RETURNING and DROP COLUMN set the floor; sqlite3.sqlite_version says what your Python links. |
| DuckDB | 1.0 and later | In process | queries, writes, transactions, migrations | In-process, so no container and no advisory lock. Releases before 1.0 are not claimed. |
| AWS Athena | engine version 3 and later | Your AWS account | queries | Your AWS account, with a staging S3 directory. Iceberg MERGE sets the floor at engine version 3. Athena's migration surface is not exercised, since the tables it needs live in your buckets. |
| ANSI (default) | Any | Nothing to run | SQL text only | The portable compiler. Any DB-API 2.0 driver that takes this SQL will work, untested here. |

<!-- end databases -->

The **Covered** column says what the suite exercises. `queries` is building
and executing SELECT, INSERT, UPDATE, and DELETE. `migrations` is the
migration lifecycle: `migrate`, `rehearse`, `down`, `validate`, and `repair`,
plus reading the schema back.

## Database versions

The **Versions** column makes two claims, and they are different kinds of claim.

The floor is the oldest release of that server on which every statement Sustained generates is valid. It is a property of the compiler: each dialect's floor is set by the newest SQL construct it emits, and the row's note names that construct. On a release older than the floor, the statements that use that construct fail in the server, and everything else works. The floor moves only in a major release of Sustained, because code running against an old server depends on it.

The suite version is the one release the integration tests run against. It is the oldest release the vendor still supports, because that is where a compatibility problem shows up first. When the vendor ends support for it, the suite version moves to the next release in a minor release of Sustained, and the changelog says which version left.

A release between the floor and the suite version takes the same SQL the suite version does, so it is inside the claim. A release newer than the suite version is expected to work.

To check your exact version, point the suite at your server and run it:

```console
$ SUSTAINED_TEST_POSTGRES_DSN=postgresql://user:pass@host/db python3 matrix.py postgres
ran     postgres    10 tests, queries, migrations
```

A clean run is the same evidence the table rests on, produced against your release. [Run it yourself](#run-it-yourself) describes the runner.

SQLite has no server: the release that matters is the one your Python links, and `sqlite3.sqlite_version` prints it.

## Python versions

<!-- python: generated from support.json -->

Sustained runs on CPython 3.9 and later. The test suite runs on 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14.

<!-- end python -->

A Python version stays supported until CPython itself stops supporting it.
After that it is dropped in a minor release, and the release before that one
says it is coming. Dropping a Python version never waits for a major
release: holding the floor down would cost every other user the newer
language features.

## Run it yourself

Nothing here has to be taken on trust. `matrix.py` in the repository reads
the same `support.json` this page comes from, starts each server, and runs
the integration suite against it:

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

Containers come from `docker/compose.yaml` and are removed at the end, so
you never type a docker command. Name a target to run one server, use
`python` for the interpreter matrix, and `--check` to see what this machine
can serve before anything starts:

```console
$ python3 matrix.py postgres
$ python3 matrix.py python
$ python3 matrix.py --check
```

A server the machine cannot serve is a `waiting` line naming the missing
piece, usually a driver to install. A server the runner did start is not
allowed to skip: a skipped test there is reported as a failure, since the
whole point is that the row ran. Exit codes are 0 for a clean run, 1 for a
failure, and 2 when nothing failed and something was still waiting.

To use a server you already run, set its connection variable, for example
`SUSTAINED_TEST_POSTGRES_DSN`, and no container is started for it. Athena
runs in your own AWS account: point `SUSTAINED_TEST_ATHENA_S3_DIR` at a
staging directory and name a profile with `--athena-profile`.

## Deprecation

A public name is anything the documentation names: a class, a function, a
method, a keyword argument, a CLI command, a CLI flag, or an exit code.

When a public name goes away, it goes in three steps.

1. **Warn.** The name keeps working and raises a `DeprecationWarning` that
   names its replacement. The changelog entry says the same thing.
2. **Wait.** At least one minor release ships with the warning in place.
3. **Remove.** The name goes in the next major release, and only there.

`sync()` is the worked example. It was replaced by `up(models=[...])` in
2.13.0, still runs, warns, and goes in 3.0.

A database losing its server, and with it everything in its Covered column,
follows the same rule: the release before the move says it is coming, and
the changelog says why.

## What a version number promises

- A **patch** release fixes a defect. Working code keeps working, and the
  SQL that comes out is the same, unless the SQL was the defect.
- A **minor** release adds behaviour, deprecates a name, drops an unsupported
  Python version, or moves a server version forward. Working code keeps
  working, warnings may be new.
- A **major** release removes deprecated names and may change behaviour that
  working code depends on. The changelog lists every removal.

Generated SQL is part of the promise. A statement that changes shape between
patch releases is a defect, not a refinement.

## Security problems

Report a security problem through a private advisory on the
[GitHub repository](https://github.com/wetherc/sustained/security/advisories),
not a public issue. Fixes ship in a patch release against the newest minor
version. Older minor versions are not patched.
