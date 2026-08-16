# Sustained.py

A Python query builder, lightweight ORM, and schema migration tool, inspired by [Objection.js](https://vincit.github.io/objection.js/).

Your models declare their columns. Sustained diffs them against the live database, writes the migration, and runs the whole thing forwards and back on a rehearsal before it touches the real schema:

```console
$ sustained rehearse
rehearsed 003_sessions  up ok, down ok, reversed
rehearsed 004_trim      up ok, down ok, reversed
rollback complete, database unchanged
```

The same model classes build and run your queries:

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## Migrations

- **Generated from your models.** `Migrator.up(models=[...])` diffs the live database, generates the migration, records it, applies it, and `down()` rolls it back. Only the difference is applied on each run. `sustained migrate` does the same from the shell.
- **Rehearsed before they land.** `sustained rehearse` applies every pending migration, runs the down steps back down, and rolls the whole thing back. It reads the schema before and after, so a migration that does not run, that does not put the models in place, or that does not reverse says so while the real schema is still untouched. Point it at a scratch database when the rollback cannot be trusted.
- **Planned in one screen.** `sustained plan` merges pending migrations, validation problems, and model drift, labels destructive statements, and exits 2 when work is waiting. `--json` for a pipeline.
- **Checked, not trusted.** The tracking table holds a sequence number, a SHA-256 checksum, timing, and a success flag per migration. `validate()` blocks a run when a migration was edited after it ran, arrives out of order, or left a failed attempt; `repair()` fixes the bookkeeping.
- **Proved before they can drop anything.** A passing rehearsal writes a receipt keyed to the exact statements it ran. `migrate` refuses a drop, a column drop, or a truncate until a receipt covers it, and `--unrehearsed` is the recorded override.
- **Held to your own rules.** Guards read every statement a run would apply and return a verdict: `no_drops()`, `index_must_be_concurrent()`, `max_statements(50)`, or a function you write. A block stops `migrate` before the first statement; `plan` prints the verdicts beside the pending work.
- **Safe by refusal.** Drops need `allow_drops=True`, renames need hints, NOT NULL changes need a `backfill`. Constraint drift is reported, never silently migrated.
- **Yours to write.** Migrations can be Python objects, `<id>.up.sql` and `<id>.down.sql` files with `${placeholders}`, or `<id>.repeat.sql` files that re-run whenever their contents change. `script('up')` renders every statement for offline review.
- **Ready for deploys.** The `sustained` console script runs `plan`, `status`, `rehearse`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline` from the shell, with exit codes and config module callbacks around each run. Concurrent deploys queue on an advisory lock. `baseline` adopts a database that already has the schema.

## What else it does

- **SQL building** for the default (ANSI), Postgres, MSSQL, Presto, AWS Athena, and DuckDB dialects: joins, CTEs (including recursive), unions, window functions, CASE expressions, and subqueries. Features a dialect lacks raise `DialectError` at build time.
- **Safe execution**: every statement runs parameterized. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Writes**: `insert()`, `update()`, `delete()`, upserts with `onConflict()`, `INSERT ... SELECT`, CTAS, and RETURNING.
- **Typed filters**: `User.query().where((User.c.age > 21) & User.c.name.like('A%'))`.
- **Results as** model instances, dicts, pandas DataFrames, or pyarrow Tables, with `withGraphFetched()` eager loading.
- **Async**: the same queries run through driver adapters (`asyncpg`, `aiosqlite`, or any sync driver in a worker thread) with `await query.arun()`, including an `AsyncMigrator`.

## What it does not do

No lazy loading, no dirty tracking or `save()`, no identity map, no result caching, no cross-dialect emulation of missing features, and no guessed migrations: drops, renames, and NOT NULL backfills all require explicit opt-ins or hints. Writes and schema changes only happen when you spell them out.

## Installation

```bash
python3 -m pip install sustained
```

## Usage

```python
from sustained import Model, RelationType

class Person(Model):
    tableName = 'persons'

class Animal(Model):
    tableName = 'animals'
    relationMappings = {
        'owner': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': Person,
            'join': {
                'from': 'animals.ownerId',
                'to': 'persons.id'
            }
        }
    }

# Build a query
query = Animal.query().select('animals.name', 'persons.name').leftOuterJoinRelated('owner')

print(query)
# SELECT animals.name, persons.name
# FROM animals
# LEFT OUTER JOIN persons
#   ON animals.ownerId = persons.id


# Execute against any DB-API 2.0 connection
import sqlite3

conn = sqlite3.connect('app.db')
Animal.bind(conn)

# Parameterized execution with model hydration
animals = Animal.query().where('species', '=', 'dog').orderBy('name').run()

# Or take the SQL and parameters and execute them yourself
sql, params = Animal.query().where('species', '=', 'dog').to_sql()
# sql:    "SELECT * FROM animals WHERE species = ?"
# params: ('dog',)
```

Models carry their own schema, so a column change is a migration:

```python
from sustained.migrations import Migrator
from sustained.schema import Integer, String, Text

class User(Model):
    tableName = 'users'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'email': String(120, unique=True, nullable=False),
    }

migrator = Migrator(conn, [])
migrator.up(models=[User])         # creates the users table

User.tableColumns['bio'] = Text()
migrator.plan([User])              # the migration the next run would generate
migrator.up(models=[User])         # adds only the bio column
migrator.down()                    # rolls it back
```

From the shell, a config module names the connection, the migrations directory, and the models:

```python
# sustained_config.py
import sqlite3

def get_connection():
    return sqlite3.connect('app.db')

migrations_dir = 'migrations'
models = [User]
```

```console
$ sustained plan        # pending migrations, validation problems, model drift
$ sustained rehearse    # run it all, forwards and back, then roll back
$ sustained migrate     # apply it for real
$ sustained down        # --steps N or --to ID
```

See [Schema and Migrations](https://sustained.tbmh.org/schema) for SQL file migrations, repeatables, checksum validation, `baseline`, and the Athena rules.

## Documentation

The [documentation](https://sustained.tbmh.org/) has four parts:

- [Getting Started](https://sustained.tbmh.org/getting-started) builds a working application in one sitting, against SQLite from the standard library.
- [Recipes](https://sustained.tbmh.org/recipes) pairs a task with the code that does it and the thing that will bite you.
- The guides cover one area each: [models](https://sustained.tbmh.org/models), [queries](https://sustained.tbmh.org/queries), [dialects and drivers](https://sustained.tbmh.org/dialects), [filtering](https://sustained.tbmh.org/filtering), [grouping](https://sustained.tbmh.org/grouping), [relations and joins](https://sustained.tbmh.org/relations), [execution, pooling, and async](https://sustained.tbmh.org/executing), and [schema and migrations](https://sustained.tbmh.org/schema) at length.
- The [API reference](https://sustained.tbmh.org/reference/) gives every public name its signature, return type, and the conditions that raise.

Released versions are listed in the [changelog](https://sustained.tbmh.org/changelog).

## Development

To install from source:

```bash
git clone https://github.com/wetherc/sustained.git
cd sustained
python3 -m pip install -e .
```

This project uses `pre-commit` to format code, lint, type check, and run the test suite before each commit:

```bash
pip install pre-commit
pre-commit install
```
