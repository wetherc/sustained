# Objection.py

A Python query builder inspired by [Objection.js](https://vincit.github.io/objection.js/).

## Installation

This package is not available on PyPI and must be installed from source.

```bash
# From within the objection directory:
pip install .
```

## Usage

```python
from objection import Model, RelationType, create_model

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
# SELECT animals.name, persons.name FROM animals LEFT OUTER JOIN persons ON animals.ownerId = persons.id
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

Now, the pre-commit hooks (including `black`, `isort`, `mypy`, and unit tests) will run automatically on every commit.
