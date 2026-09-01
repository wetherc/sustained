# Sustained.py

Sustained is a Python query builder, lightweight ORM, and schema migration tool, originally inspired by [Objection.js](https://vincit.github.io/objection.js/).

With Sustained, You define one set of model classes to describe your tables, and Sustained builds and runs the queries against them, and keeps the schema itself in step.

The syntax will look familiar if you have worked with Objection, Kysely, or even knex before:

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## Managing queries through Sustained

With Sustained, you can:

- **Build SQL programmatically.** Selects, aggregates, window functions, CASE expressions, every join type, CTEs (including recursive), unions, INTERSECT and EXCEPT, subqueries in SELECT, FROM, WHERE, and JOIN clauses.
- **Target seven dialects.** ANSI (default), PostgreSQL, MySQL and MariaDB, MSSQL, Presto, AWS Athena, and DuckDB. Quoting, placeholders, upsert syntax, LIMIT/OFFSET spelling, and function names all follow the dialect. Unsupported features raise `DialectError` at build time instead of failing in the database. Migrating queries between dialects is a one-line change.
- **Execute queries safely.** Every statement runs parameterized against any DB-API 2.0 connection or a `ConnectionPool`. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Write data.** `insert()`, `update()`, `delete()`, upserts through `onConflict()`, `INSERT ... SELECT`, CREATE TABLE AS, and RETURNING.
- **Hydrate results.** Rows become model instances, plain dicts, pandas DataFrames, or pyarrow Tables. Relations eager load with `withGraphFetched()`. A type checker reads `Show.query().run()` as `List[Show]`.
- **Run queries async.** The same queries run through driver adapters with `await query.arun()`, including asyncpg and aiosqlite. `AsyncConnectionPool` pools those adapters, so concurrent queries do not queue behind one connection.

## Schema management with Sustained

Sustained also provides strong support for database schema change management, to allow you easily and reliably test schema changes safely, evolve your database schema, and easily roll back migrations. These features are discussed in detail at [Schema and Migrations](https://sustained.tbmh.org/schema).

With Sustained, schema migrations are:

- **Generated from your models.** `Migrator.up(models=[...])` diffs the live database against your models, generates the migration, records it, and applies it. Run it again after a model change and only the difference is applied. `down()` rolls it back.
- **Rehearsed before they land.** `sustained rehearse` applies every pending migration, runs the downgrade steps to test the revert plan, and rolls the whole thing back. A migration that does not run, or does not reverse, says so before it reaches the real schema. A config module can send the rehearsal to a scratch database instead.
- **Planned in one screen.** `sustained plan` shows your pending migrations, outstanding problems that `validate` would report, and any gap between your models and the database's current state.
- **Checked, not trusted.** Sustained manages a per-database tracking table that holds a sequence number, a SHA-256 checksum, an apply timestamp, execution time, and a success flag per migration. `validate` refuses a run when a migration was edited after it ran, arrives out of order, or left a failed attempt behind. `repair` will delete failed runs from the tracking table and update script checksums after manual corrections.
- **Gated by custom safeguards.** Guards can be built-in functions (`no_drops()`, `index_must_be_concurrent()`, `max_statements(n)`) or can be a custom function you write. These read every statement of a migration that would run and block the deployment if any of the rule checks fail.
- **Safe by default.** Drops need explicit `allow_drops=True`, renames need explicit hints, NOT NULL changes need a `default` or `backfill`. Destructive changes will never run by default.
- **Written your way.** Migrations can be Python `Migration` objects, `<id>.up.sql` and `<id>.down.sql` files with `${placeholders}`, or `<id>.repeat.sql` files for views and seed data, which re-run whenever their contents change.
- **Ready for deploys.** The `sustained` console script runs `plan`, `status`, `rehearse`, `migrate`, `down`, `validate`, `repair`, `script`, and `baseline`, with exit codes for pipelines and `before_migrate`, `after_migrate`, and `on_error` callbacks around a run. Concurrent deploys queue on an advisory lock. `baseline` adopts a database that already matches. `script('up')` renders the SQL for a DBA instead of running it. `AsyncMigrator` does all of it on an async adapter.


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

Supported databases and Python versions, and version deprecation/removal policy, are documented at
[support policy](https://sustained.tbmh.org/support). Released versions are
listed in the [changelog](https://sustained.tbmh.org/changelog).

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
