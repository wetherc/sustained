# Filtering Queries

Objection.py provides a rich, dynamic API for adding `WHERE` clauses to your queries.

[<-- Back to Index](./index.md)

## Basic `where` Methods

The most common way to filter is by using the `where`, `andWhere`, and `orWhere` methods. These are handled dynamically, so you can chain them as needed.

A `where` clause takes three arguments: a column, an operator, and a value.

```python
from my_project import Movie

# SELECT * FROM movies WHERE director = 'Quentin Tarantino'
Movie.query().where('director', '=', 'Quentin Tarantino')

# Chain multiple `where` clauses
# SELECT * FROM movies WHERE release_year > 2000 AND rating > 8.5
Movie.query().where('release_year', '>', 2000).andWhere('rating', '>', 8.5)

# Use `orWhere` to add an alternative condition
# SELECT * FROM movies WHERE genre = 'Sci-Fi' OR genre = 'Fantasy'
Movie.query().where('genre', '=', 'Sci-Fi').orWhere('genre', '=', 'Fantasy')
```
> **Note:** The first `where` call in a chain cannot be an `orWhere`. It must be a plain `where` or `andWhere`.

## `whereIn` and `whereNotIn`

To filter against a list of values, use the `whereIn` and `whereNotIn` methods.

### `whereIn`

This generates a `WHERE col IN (...)` clause.

```python
# SELECT * FROM movies WHERE id IN (10, 25, 30)
Movie.query().whereIn('id', [10, 25, 30])

# You can also use `andWhereIn` and `orWhereIn`
# SELECT * FROM movies WHERE release_year = 1999 AND genre IN ('Action', 'Sci-Fi')
Movie.query().where('release_year', '=', 1999).andWhereIn('genre', ['Action', 'Sci-Fi'])
```

### `whereNotIn`

This generates a `WHERE col NOT IN (...)` clause.

```python
# SELECT * FROM movies WHERE rating NOT IN (1, 2, 3)
Movie.query().whereNotIn('rating', [1, 2, 3])
```

## Grouped `where` Clauses

For complex logical groupings, you can pass a callable (like a lambda function) to any `where` method. The function will receive a temporary `QueryBuilder` instance that you can use to build the grouped condition.

This is useful for creating conditions wrapped in parentheses, like `... AND (condition A OR condition B)`.

```python
# SELECT * FROM movies
# WHERE genre = 'Action' AND (release_year < 1990 OR rating > 9.0)

query = Movie.query().where('genre', '=', 'Action').andWhere(lambda q: (
    q.where('release_year', '<', 1990).orWhere('rating', '>', 9.0)
))
```

### Complex Grouping

You can nest these groups as deeply as you need.

```python
# SELECT * FROM movies
# WHERE status = 'available'
# AND (
#   (genre = 'Comedy' AND rating > 7) OR
#   (genre = 'Drama' AND rating > 8)
# )

query = Movie.query() 
    .where('status', '=', 'available') 
    .andWhere(lambda q: (
        q.where(lambda group1: (
            group1.where('genre', '=', 'Comedy').andWhere('rating', '>', 7)
        )).orWhere(lambda group2: (
            group2.where('genre', '=', 'Drama').andWhere('rating', '>', 8)
        ))
    ))
```
