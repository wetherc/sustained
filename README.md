# Sustained.py

A Python query builder inspired by [Objection.js](https://vincit.github.io/objection.js/).

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
```

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
