---
layout: default
title: Relations and Joins
---

[<-- Back to Index](./index)

## Defining Relations

Relations are defined in a `relationMappings` dictionary on your `Model` class. Each key in the dictionary is a name for the relation, and the value is a `RelationMapping` object.

A `RelationMapping` requires three properties:
- `relation`: The type of relation (e.g., `RelationType.BelongsToOneRelation`).
- `modelClass`: The `Model` class that is being related to. This can be the class itself or a string with the class name.
- `join`: A dictionary describing how the tables are connected.

### Relation Types

Sustained supports the following relation types via the `RelationType` enum:
- `BelongsToOneRelation`: A one-to-one or many-to-one relationship.
- `HasManyRelation`: A one-to-many relationship.
- `HasOneRelation`: A special case of a one-to-one relationship.
- `ManyToManyRelation`: A many-to-many relationship that requires a `through` table.

### Example: `BelongsToOneRelation`

This is used for both one-to-one and many-to-one relations. For example, an `Animal` has one `owner` (`Person`), and a `Person` can own many `Animal`s.

```python
from sustained import Model, RelationType

class Person(Model):
    tableName = 'persons'

class Animal(Model):
    tableName = 'animals'
    relationMappings = {
        'owner': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': Person, # Using the class directly
            'join': {
                'from': 'animals.ownerId',
                'to': 'persons.id'
            }
        }
    }
```

### Example: `ManyToManyRelation`

This is for relationships that require an intermediate (or "through") table. For example, a `Person` can act in many `Movie`s, and a `Movie` has many `Person`s (actors). The link is stored in a `persons_movies` table.

```python
class Movie(Model):
    tableName = 'movies'

class Person(Model):
    tableName = 'persons'
    relationMappings = {
        'movies': {
            'relation': RelationType.ManyToManyRelation,
            'modelClass': 'Movie', # Using a string name
            'join': {
                'from': 'persons.id',
                'through': {
                    'from': {'table': 'persons_movies', 'key': 'personId'},
                    'to': {'table': 'persons_movies', 'key': 'movieId'}
                },
                'to': 'movies.id'
            }
        }
    }
```

## Joining Relations

Once relations are defined, you can use a family of `...JoinRelated` methods to add `JOIN` clauses to your query. The method name determines the type of `JOIN` performed.

- `joinRelated()` (defaults to `INNER JOIN`)
- `innerJoinRelated()`
- `leftJoinRelated()`
- `leftOuterJoinRelated()`
- `rightJoinRelated()`
- `rightOuterJoinRelated()`
- `fullOuterJoinRelated()`
- `crossJoinRelated()`

```python
# SELECT animals.name, persons.name
# FROM animals
# LEFT OUTER JOIN persons
#   ON animals.ownerId = persons.id
query = Animal.query().select('animals.name', 'persons.name').leftOuterJoinRelated('owner')
```

### Joining with an Alias

You can also provide a custom alias for the joined table, which is useful for complex queries or self-joins.

```python
# SELECT *
# FROM animals
# INNER JOIN persons AS p
#   ON animals.ownerId = p.id
query = Animal.query().select('animals.name', 'p.name').innerJoinRelated('owner', alias='p')
```

### Joins with `through` Tables

When you join a `ManyToManyRelation`, Sustained automatically handles joining the intermediate table first, followed by the final table.

```python
# SELECT *
# FROM persons
# INNER JOIN persons_movies
#   ON persons.id = persons_movies.personId
# LEFT JOIN movies
#   ON persons_movies.movieId = movies.id
query = Person.query().select('persons.name', 'movies.title').leftJoinRelated('movies')
```

Notice that the join from `persons` to `persons_movies` is an `INNER JOIN`, while the join from `persons_movies` to `movies` is the `LEFT JOIN` you requested. This is the standard, expected behavior for joining through tables.


## Raw Joins

For simple joins where you don't have or need a pre-defined relation on your model, you can use the raw join methods. These methods are generated dynamically for each join type (`join`, `innerJoin`, `leftJoin`, `rightJoin`, etc.).

They accept four arguments:
1.  The table to join to.
2.  The first column for the `ON` condition.
3.  The operator for the `ON` condition.
4.  The second column for the `ON` condition.

```python
# SELECT persons.*, animals.name
# FROM persons
# LEFT JOIN animals
#   ON persons.id = animals.ownerId
query = Person.query().leftJoin('animals', 'persons.id', '=', 'animals.ownerId')
```

### Complex Joins with Lambdas

For joins that require multiple or complex `ON` conditions, you can pass a lambda function as the second argument to any of the `join` methods. This lambda receives a `JoinBuilder` object that you can use to construct the join conditions.

The `JoinBuilder` has the following methods:
*   `on(col1, op, col2)`: Adds the initial `ON` condition.
*   `andOn(col1, op, col2)`: Adds an `AND` condition to the join.
*   `orOn(col1, op, col2)`: Adds an `OR` condition to the join.

```python
# SELECT *
# FROM users
# JOIN accounts
#   ON accounts.id = users.account_id
#   OR accounts.owner_id = users.id
query = User.query().join(
    'accounts',
    lambda j: j.on('accounts.id', '=', 'users.account_id')
    .orOn('accounts.owner_id', '=', 'users.id'),
)
```

Note that more complex nested join criteria are not supported.
