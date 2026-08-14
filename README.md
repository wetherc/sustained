# Sustained.py

A Python query builder and lightweight ORM inspired by [Objection.js](https://vincit.github.io/objection.js/).

Sustained builds parameterized SQL for the default (ANSI), Postgres, MSSQL, Presto, and DuckDB dialects. It executes queries against any DB-API 2.0 connection or connection pool with transactions, hydrates rows into model instances or DataFrames, writes data with `insert()`, `update()`, `delete()`, upserts, and `INSERT ... SELECT`, and eager loads relations. Filters compose as typed predicates: `User.query().where((User.c.age > 21) & User.c.name.like('A%'))`. Models declare typed columns and manage their own schema: `Migrator.sync(models)` diffs the live database, generates the migration, applies it, and `down()` rolls it back, with destructive changes gated behind explicit opt-ins. Async services run the same queries through driver adapters with `await query.arun()`.

## Installation

```bash
python3 -m pip install sustained
```

## Local Installation from Source

To install `sustained` from source for local development:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/wetherc/sustained.git
    cd sustained
    ```

2.  **Install in editable mode:**
    ```bash
    python3 -m pip install -e .
    ```

## Usage

```python
from sustained import Model, RelationType, create_model

class Person(Model):
    database = 'my_db'
    tableSchema = 'public'
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


# Build a more complex query with a CTE and a raw join
active_owners = Person.query().select('id').where('status', '=', 'active')

query = (
    Animal.query()
    .with_('active_owners', active_owners)
    .join('active_owners', 'animals.ownerId', '=', 'active_owners.id')
    .select('animals.name')
)

print(query)
# WITH active_owners AS (
#   SELECT id
#   FROM persons
#   WHERE status = 'active'
# )
# SELECT animals.name
# FROM animals
# JOIN active_owners
#   ON animals.ownerId = active_owners.id


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

See the [documentation](https://github.com/wetherc/sustained/tree/main/docs) for models, filtering, grouping, relations, and execution guides.

## Development

This project uses `pre-commit` to enforce code quality and run tests before committing code.

### Pre-commit Hooks Setup

1.  **Install pre-commit:**
    ```bash
    pip install pre-commit
    ```

2.  **Install the Git hooks:**
    From the root of the project directory, run:
    ```bash
    pre-commit install
    ```
