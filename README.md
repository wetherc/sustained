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
