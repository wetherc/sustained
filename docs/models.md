---
layout: default
title: Defining Models
---


The `Model` class is the foundation of sustained.py. Each model you create represents a database table.

[<-- Back to Index](./index)

## Basic Setup

To define a model, create a class that inherits from `sustained.Model` and give it a `tableName`.

```python
from sustained import Model

class Person(Model):
    # This is the only required property.
    tableName = 'persons'

class Animal(Model):
    tableName = 'animals'
```

### Namespace Properties

For fully qualified table names, you can also specify `database` and `tableSchema`.

```python
class User(Model):
    database = 'my_db'
    tableSchema = 'public'
    tableName = 'users'

# This model will produce queries like:
# SELECT * FROM my_db.public.users
print(User.query())
```
These properties are used by the `QueryBuilder` to construct the `FROM` clause of your SQL queries.

## Dynamic Model Creation

In some cases, you might need to create models at runtime. The `create_model` function is provided for this purpose. It takes the desired class name and table name as arguments.

```python
from sustained import create_model, RelationType

# A simple dynamic model
Vehicle = create_model('Vehicle', 'vehicles')

# You can use it immediately
query = Vehicle.query().select('id', 'license_plate')
print(query)
# SELECT id, license_plate FROM vehicles
```

You can also define relations for dynamic models:
```python
Person = create_model('Person', 'persons')

Animal = create_model(
    name='Animal',
    table_name='animals',
    mappings={
        'owner': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': Person,
            'join': {'from': 'animals.ownerId', 'to': 'persons.id'}
        }
    }
)
```

This works just like a statically defined model
```python
query = Animal.query().innerJoinRelated('owner')
print(query)
# SELECT *
# FROM animals
# INNER JOIN persons
#   ON animals.ownerId = persons.id
```

## Column Name Access

Model instances provide a convenient way to get fully-qualified column names for use in queries, which helps avoid ambiguity.

```python
person = Person()

# Accessing an attribute on a model instance returns the qualified column name
print(person.id)
# "persons.id"

# Use it in a select statement
query = Person.query().select(person.firstName, person.lastName)
print(query)

# SELECT persons.firstName, persons.lastName
# FROM persons
```

If the model has a `database` or `tableSchema` defined, they will be included in the qualified name.

```python
user = User() # The model was instantiated with a `database` and `tableSchema`
print(user.id)
# "my_db.public.users.id"
```
