# Sustained.py

A Python query builder and lightweight ORM inspired by [Objection.js](https://vincit.github.io/objection.js/).

You describe a query as chained Python methods. Sustained renders the SQL for your dialect, runs it parameterized against any DB-API 2.0 connection or pool, and hydrates the rows into your model classes.

```python
adults = User.query().where(User.c.age >= 18).orderBy('name').run()
```

## What it does

- **SQL building** for the default (ANSI), Postgres, MSSQL, Presto, AWS Athena, and DuckDB dialects: joins, CTEs (including recursive), unions, window functions, CASE expressions, and subqueries. Features a dialect lacks raise `DialectError` at build time.
- **Safe execution**: every statement runs parameterized. Transactions nest through savepoints. `update()` and `delete()` refuse to run without a WHERE clause.
- **Writes**: `insert()`, `update()`, `delete()`, upserts with `onConflict()`, `INSERT ... SELECT`, CTAS, and RETURNING.
- **Typed filters**: `User.query().where((User.c.age > 21) & User.c.name.like('A%'))`.
- **Results as** model instances, dicts, pandas DataFrames, or pyarrow Tables, with `withGraphFetched()` eager loading.
- **Schema migrations**: models declare typed columns and indexes; `Migrator.sync(models)` diffs the live database, generates the migration, applies it, and `down()` rolls it back. Destructive changes are gated behind explicit opt-ins.
- **Async**: the same queries run through driver adapters (`asyncpg`, `aiosqlite`, or any sync driver in a worker thread) with `await query.arun()`.

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

Models can manage their own schema:

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
migrator.sync([User])              # creates the users table

User.tableColumns['bio'] = Text()
migrator.sync([User])              # adds only the bio column
migrator.down()                    # rolls it back
```

## Documentation

The [documentation](https://sustained.tbmh.org/) covers [models](https://sustained.tbmh.org/models), [queries](https://sustained.tbmh.org/queries), [dialects and drivers](https://sustained.tbmh.org/dialects), [filtering](https://sustained.tbmh.org/filtering), [grouping](https://sustained.tbmh.org/grouping), [relations and joins](https://sustained.tbmh.org/relations), [execution, pooling, and async](https://sustained.tbmh.org/executing), and [schema and migrations](https://sustained.tbmh.org/schema). The [API reference](https://sustained.tbmh.org/reference) lists every public method by task.

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
