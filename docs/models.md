---
layout: default
title: Defining Models
---


The `Model` class is the foundation of sustained.py. Each model you create represents a database table.



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

Models provide a convenient way to get fully-qualified column names for use in queries, which helps avoid ambiguity. Access a column as an attribute on the model class itself. Instances work the same way.

```python
# Accessing an attribute on the model class returns the qualified column name
print(Person.id)
# "persons.id"

# Use it in a select statement
query = Person.query().select(Person.firstName, Person.lastName)
print(query)

# SELECT persons.firstName, persons.lastName
# FROM persons
```

If the model has a `database` or `tableSchema` defined, they will be included in the qualified name.

```python
print(User.id)
# "my_db.public.users.id"
```

## Declared Columns

By default, any attribute name resolves to a column string, so a typo becomes a bad column name in your SQL. To catch typos early, declare the model's columns. Access to an undeclared column then raises an `AttributeError` that lists the declared set.

```python
class Person(Model):
    tableName = 'persons'
    columns = ('id', 'firstName', 'lastName')

Person.id          # "persons.id"
Person.firstNam    # AttributeError
```

The `create_model` function accepts the same declaration through its `columns` argument. Models that declare typed columns in `tableColumns` (see [Schema and Migrations](./schema)) get the strict column set derived automatically.

## Typed Column References

Beyond plain name strings, every model exposes a typed column namespace at `Model.c`. Python comparison operators on these references build `Predicate` objects for `where()` and `having()`:

```python
User.query().where((User.c.age > 21) & User.c.name.like('A%'))
```

See [Filtering](./filtering#typed-predicates) for the full operator set.

## The Model Registry

Every model subclass with a `tableName` registers itself under its class name. Relation mappings can therefore reference a related model by name, even when the two models live in different modules. See [Relations and Joins](./relations).

## Binding a Connection

Models can execute their queries when you bind a DB-API connection with `Model.bind()`. See [Executing Queries](./executing).
