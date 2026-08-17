---
layout: default
title: Support Policy
---

This page says which databases and Python versions Sustained supports, what
support means, and how much warning you get before something goes away. The
tables come from `support.json` in the repository, and a pre-commit hook
fails when this page and that file disagree. The claim on this page and the
list the test suite reads are the same list.

## Two levels of support

A database is at one of two levels.

**`runs`** means the integration suite applies migrations to a real server of
that database and reads the schema back afterwards. A container starts, the
tests run against it, and the container stops. SQLite and DuckDB run in
process, and Athena runs in your own AWS account, but the suite is the same
suite.

**`builds`** means Sustained compiles SQL for it, and unit tests check the SQL
text. Nothing executes. The SQL is correct as far as the compiler is
concerned, and your driver is where you find out the rest.

Nothing sits between the two. A database Sustained cannot run against is
never described as partly supported.

<!-- databases: generated from support.json -->

| Database | Level | Where it runs | Covered | Notes |
| --- | --- | --- | --- | --- |
| PostgreSQL | `runs` | Container (postgres:14-alpine) | queries, migrations | Rehearsal, advisory locks, and transactional DDL all run here. |
| MySQL | `runs` | Container (mysql:8.4) | queries, migrations | No transactional DDL, so rehearse needs scratch=True. |
| MariaDB | `runs` | Container (mariadb:11.4) | queries, migrations | Same dialect as MySQL. JSON columns read back through their json_valid check. |
| SQL Server | `runs` | Container (mcr.microsoft.com/mssql/server:2022-latest) | queries, migrations | Off the rehearsal allowlist, so rehearse refuses without scratch=True. pyodbc needs the ODBC driver installed. |
| Presto and Trino | `runs` | Container (trinodb/trino:468) | queries | Reads the tpch catalog. Neither server takes the migration surface. |
| SQLite | `runs` | In process | queries, migrations | Standard library. The table rebuild path lives here. |
| DuckDB | `runs` | In process | queries, migrations | In-process, so no container and no advisory lock. |
| AWS Athena | `runs` | Your AWS account | queries | Your AWS account, with a staging S3 directory. Athena's migration surface is not exercised, since the tables it needs live in your buckets. |
| ANSI (default) | `builds` | Nothing to run | SQL text only | The portable compiler. Any DB-API 2.0 driver that takes this SQL will work, untested here. |

<!-- end databases -->

The **Covered** column says what the suite exercises. `queries` is building
and executing SELECT, INSERT, UPDATE, and DELETE. `migrations` is the
migration lifecycle: `migrate`, `rehearse`, `down`, `validate`, and `repair`,
plus reading the schema back.

## Database versions

One version of each server runs, and the table names it. It is the oldest
release the vendor still supports, because that is where a compatibility
problem shows up first. A newer release is expected to work and is not
tested here.

When a vendor ends support for the version in the table, the table moves to
the next one in the same minor release of Sustained. The changelog says which
version left.

## Python versions

<!-- python: generated from support.json -->

Sustained runs on CPython 3.9 and later. The test suite runs on 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14.

<!-- end python -->

A Python version stays supported until CPython itself stops supporting it.
After that it is dropped in a minor release, and the release before that one
says it is coming. Dropping a Python version never waits for a major
release: holding the floor down would cost every other user the newer
language features.

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

A database moving from `runs` down to `builds` follows the same rule: the
release before the move says it is coming, and the changelog says why.

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
