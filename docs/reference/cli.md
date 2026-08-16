---
layout: default
title: Command line reference
---

The `sustained` console script, installed with the package. It also runs as
`python -m sustained`.

```
sustained <command> [--config MODULE] [command options]
```

Every command imports a config module, `sustained_config` by default, from the
current directory. Every command accepts `--config MODULE` to name another
one.

Guide: [Schema and Migrations](/schema#command-line).

## Commands

| Command | Options | Does |
| --- | --- | --- |
| `plan` | `--json` | Shows the pending migrations, the problems, and the model drift. |
| `status` | `--json` | Shows every migration's state: applied, pending, or changed. |
| `rehearse` | `--json` | Runs the pending migrations up and back down, then rolls it all back. |
| `migrate` | `--target ID`, `--no-validate`, `--allow-out-of-order`, `--unrehearsed` | Applies pending migrations in order. |
| `down` | `--steps N` (default 1) or `--to ID` | Reverts applied migrations, newest first. |
| `validate` | `--json` | Checks the tracking table against the migrations. |
| `repair` | | Fixes tracking rows after failures or intentional edits. |
| `script` | `up` or `down` (default `up`) | Prints the SQL a run would execute, without running it. |
| `baseline` | `TARGET` (required) | Records migrations as applied without running them. |

`--steps` and `--to` are mutually exclusive.

## Exit codes

| Code | Means |
| --- | --- |
| 0 | Success, or nothing to do. |
| 1 | A failure: config, connection, validation problems, or a migration error. Details on stderr. |
| 2 | `plan` only: work is waiting. |

`plan` is the one command with three outcomes: 0 when the database is current,
2 when migrations are pending or the models have drifted, and 1 when
validation found problems. Problems outrank pending work.

argparse also exits 2 on a usage error, so a script that treats 2 as "work is
waiting" should check stderr for an `error:` line.

`rehearse` exits 1 when an up or a down step failed, when the models did not
land, or when the schema did not come back. A migration with no down step is
not a failure, so it exits 0.

`migrate` exits 1 when the run would remove data and no passing rehearsal
covers those statements. The message names them and both ways forward.

`validate` exits 1 when problems exist, 0 otherwise. Exit codes are the same
with and without `--json`.

## The config module

| Attribute | Required | Shape | Default |
| --- | --- | --- | --- |
| `connection` | one of the two | A DB-API 2.0 connection | checked first |
| `get_connection` | one of the two | `() -> Connection` | used when `connection` is absent |
| `migrations` | no | `list[Migration]` | `[]` |
| `migrations_dir` | no | Path to `.up.sql` / `.down.sql` / `.repeat.sql` files | `None` |
| `placeholders` | no | `dict[str, str]` filling `${key}` markers | `None` |
| `models` | no | List of model classes | `None` |
| `dialect` | no | A `Dialects` member, or its name: `'postgres'`, `'MSSQL'` | `Dialects.DEFAULT` |
| `table` | no | Tracking table name | `'sustained_migrations'` |
| `rehearsal_table` | no | Receipt table name | `'sustained_rehearsals'` |
| `tracking_table_options` | no | `TableOptions` | `None` |
| `get_rehearsal_connection` | no | `() -> Connection`, a scratch database | `None` |
| `before_migrate` | no | `(connection) -> None` | not called |
| `after_migrate` | no | `(connection, applied) -> None` | not called |
| `on_error` | no | `(connection, migration_id, error) -> None` | not called |

Defining neither `connection` nor `get_connection` raises `ValueError`. An
unknown dialect name raises `ValueError` listing the six valid ones.

Migrations from `migrations_dir` are appended after `migrations`, so both
sources can coexist.

```python
# sustained_config.py
import psycopg

from models import Show, Venue


def get_connection():
    return psycopg.connect('postgresql://localhost/app')


migrations_dir = 'migrations'
models = [Venue, Show]
dialect = 'postgres'
```

### Callbacks

Only `migrate` calls them. `rehearse` does not, because nothing real happened.

`before_migrate` runs before the run starts, which is before validation and
before the advisory lock. `after_migrate` runs only when at least one
migration applied, so a run with nothing to do stays quiet. `on_error` runs
after a failure and before it reaches the shell; `migration_id` is `None` when
the run failed before reaching a migration.

A callback that is not callable is skipped. An `on_error` that raises has its
own error printed to stderr, and the original migration error still decides
the exit code.

### Rehearsal connection

When the config defines `get_rehearsal_connection()`, `rehearse` builds a
second migrator on that connection and rehearses there instead. The dialect
check does not apply, the changes may survive the rollback, and the footer
says so. The scratch connection closes when the command ends.

The receipt goes on the real database, not the scratch one, keyed against the
real database's applied history and pending set. It is written only when the
scratch run applied every migration pending there; otherwise the output says
the receipt was not recorded.

## Output

Plain text, one record per line, nothing coloured.

```console
$ sustained status
applied  001_create_venues
pending  002_create_shows
changed  upcoming_shows
```

```console
$ sustained plan
pending
  003_sessions  2 statements
  004_trim      1 statement
    destructive  ALTER TABLE users DROP COLUMN legacy
  vw_active     1 statement  repeat changed

drift
  ALTER TABLE users ADD COLUMN bio TEXT

2 pending migrations, 1 drift statement
run: sustained rehearse
```

The footer points at `rehearse` when a pending migration removes data, since
`migrate` refuses those without a receipt, and at `migrate` otherwise.

The drift section appears only when the config names `models`. It reports
every difference, drops included, while `migrate` never generates a drop; a
drift section holding only drops says so instead of offering the command. The
`run:` line prints only when there are no problems.

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down ok, reversed
rehearsed 004_trim      up ok, down ok, reversed
rehearsed vw_active     up ok, no down step (repeatable)
rollback complete, database unchanged
receipt recorded
```

`receipt recorded` means the proof was written where `migrate` will read it.
The words after the id are the proofs, in order: `up ok`, `landed` for the
migration generated from the config's `models`, `down ok`, and `reversed`. A
check that failed reads `not landed` or `not reversed`, with the objects listed
underneath and `run: sustained plan` at the end.

A failure names the statement that failed and the migrations under it that
never got their turn:

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down not rehearsed: the run stopped
failed    004_trim      up: column "legacy" of relation "users" does not exist
rollback complete, database unchanged
run: sustained plan
```

When the config names `models`, `migrate` reads the schema back after a
successful run and prints `schema matches the models`, or one `drift    <gap>`
line per difference left. It is a report; the exit code does not change.

Other commands print `applied  <id>`, `reverted <id>`, `repaired <action>`, or
`baselined <id>`, one per line, and `Nothing to apply.`, `Nothing to revert.`,
`Nothing to repair.`, `Nothing to baseline.`, or `Nothing to rehearse.` when
there was nothing to do. `validate` prints `OK` or one `problem  <text>` line
per problem.

Errors go to stderr as `error: <message>`, or
`error in '<migration id>': <message>` when the failure came from a known
migration.

## JSON output

`status`, `validate`, `plan`, and `rehearse` take `--json` and print one object
to stdout.

```console
$ sustained plan --json
{
  "pending": [
    {
      "id": "004_trim",
      "state": "pending",
      "repeatable": false,
      "statements": [
        {
          "sql": "ALTER TABLE users DROP COLUMN legacy",
          "destructive": true
        }
      ],
      "destructive": ["ALTER TABLE users DROP COLUMN legacy"]
    }
  ],
  "problems": [],
  "drift": null
}
```

Every place a command reports SQL uses that statement object, `drift`
included. `drift` is `null`, not `[]`, when the config names no models, so a
caller can tell "nothing was compared" from "compared and found no gap".
`statements` is `null` for a callable step, which renders no SQL. Before
version 2.13.0 it was a count.

`rehearse --json` prints:

```console
$ sustained rehearse --json
{
  "rehearsed": [
    {
      "id": "004_trim",
      "up_ok": true,
      "down_ok": true,
      "error": null,
      "landed": null,
      "reversed": []
    }
  ],
  "scratch": false,
  "key": "9c1f...",
  "recorded": true,
  "ok": true
}
```

`landed` and `reversed` are `null` when the check did not run, `[]` when it
passed, and the lines naming the trouble when it failed. `key` names the
content the run covered; `recorded` says whether the receipt was written where
`migrate` will read it.

`status --json` prints `{"migrations": [{"id": ..., "state": ...}]}`.
`validate --json` prints `{"ok": ..., "problems": [...]}`.
