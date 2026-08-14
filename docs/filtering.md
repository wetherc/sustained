---
layout: default
title: Filtering Queries
---



## Basic `where` Methods

The most common way to filter is by using the `where`, `andWhere`, and `orWhere` methods. These are handled dynamically, so you can chain them as needed.

A `where` clause takes three arguments: a column, an operator, and a value.

```python
from my_project import Movie

# SELECT *
# FROM movies
# WHERE director = 'Quentin Tarantino'
Movie.query().where("director", "=", "Quentin Tarantino")

# Chain multiple `where` clauses
#   SELECT *
#   FROM movies
#   WHERE release_year > 2000
#     AND rating > 8.5
Movie.query().where("release_year", ">", 2000).andWhere("rating", ">", 8.5)

# Use `orWhere` to add an alternative condition
#   SELECT *
#   FROM movies
#   WHERE genre = 'Sci-Fi'
#      OR genre = 'Fantasy'
Movie.query().where("genre", "=", "Sci-Fi").orWhere("genre", "=", "Fantasy")
```
> **Note:** The first `where` call in a chain cannot be an `orWhere` or `andWhere`. It must be a plain `where`.

### Operators

The operator must be one of the recognized comparison operators: `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`, `ILIKE`, `NOT ILIKE`, `IS`, `IS NOT`. Anything else raises a `ValueError`. For a raw predicate, use `QueryBuilder.raw()`.

### Comparing with None

Comparing a column to `None` with `=` or `!=` renders `IS NULL` or `IS NOT NULL`.

```python
# SELECT * FROM movies WHERE tagline IS NULL
Movie.query().where("tagline", "=", None)
```

### Values and Parameterization

Column names quote per dialect when they are plain identifier paths. Values render as SQL literals in `str(query)` and as placeholders in `to_sql()`. See [Executing Queries](./executing).

## Typed Predicates

Every model exposes a typed column namespace at `Model.c`, and the `col()` helper wraps any dotted path. Python comparison operators build `Predicate` objects, which combine with `&` (AND), `|` (OR), and `~` (NOT). Pass the result to `where()` or `having()`.

```python
from sustained import col

# SELECT * FROM movies WHERE ((rating > 8 AND genre = 'Sci-Fi') OR NOT (archived = TRUE))
Movie.query().where(
    (Movie.c.rating > 8) & (Movie.c.genre == "Sci-Fi") | ~(Movie.c.archived == True)
)
```

Column methods cover the rest of the operators: `.like()`, `.not_like()`, `.ilike()`, `.in_()`, `.not_in()`, `.between()`, `.not_between()`, `.is_null()`, `.not_null()`. Values parameterize in `to_sql()` and columns quote per dialect. Comparing with `== None` or `!= None` renders `IS NULL` / `IS NOT NULL`. A `Predicate` raises `TypeError` in a boolean context, which catches accidental use of the `and`/`or` keywords.

## Raw Predicates with Bound Values: `whereRaw`

For a predicate the builder cannot express, use `whereRaw(sql, params)`. Mark each value with `?`; the values travel as parameters, never as text in the SQL. `havingRaw` works the same way for HAVING.

```python
# SELECT * FROM movies WHERE (release_year % ? = ?)
Movie.query().whereRaw("release_year % ? = ?", [2, 0])
```

The marker count must match the parameter count, and the fragment renders wrapped in parentheses.

## `whereIn` and `whereNotIn`

To filter against a list of values, use the `whereIn` and `whereNotIn` methods.

### `whereIn`

This generates a `WHERE col IN (...)` clause.

```python
# SELECT * FROM movies WHERE id IN (10, 25, 30)
Movie.query().whereIn("id", [10, 25, 30])

# You can also use `andWhereIn` and `orWhereIn`
#   SELECT *
#   FROM movies
#   WHERE release_year = 1999
#     AND genre IN ('Action', 'Sci-Fi')
Movie.query().where("release_year", "=", 1999).andWhereIn("genre", ["Action", "Sci-Fi"])
```

### `whereNotIn`

This generates a `WHERE col NOT IN (...)` clause.

```python
# SELECT * FROM movies WHERE rating NOT IN (1, 2, 3)
Movie.query().whereNotIn("rating", [1, 2, 3])
```

### Using Subqueries with `whereIn` and `whereNotIn`

You can also provide a `QueryBuilder` instance or a callable to `whereIn` and `whereNotIn` to use a subquery.

```python
from my_project import User, BannedUser

# SELECT *
# FROM users
# WHERE id IN (
#   SELECT user_id
#   FROM banned_users
# )
User.query().whereIn(
    "id",
    BannedUser.query().select("user_id")
)
```

## Grouped `where` Clauses

For complex logical groupings, you can pass a callable (like a lambda function) to any `where` method. The function will receive a temporary `QueryBuilder` instance that you can use to build the grouped condition.

This is useful for creating conditions wrapped in parentheses, like `... AND (condition A OR condition B)`.

```python
# SELECT *
# FROM movies
# WHERE genre = 'Action'
#   AND (release_year < 1990 OR rating > 9.0)

query = Movie.query().where("genre", "=", "Action").andWhere(lambda q: (
    q.where("release_year", "<", 1990).orWhere("rating", ">", 9.0)
))
```

### Complex Grouping

You can nest these groups as deeply as you need.

```python
# SELECT *
# FROM movies
# WHERE status = 'available'
#   AND (
#     (genre = 'Comedy' AND rating > 7) OR
#     (genre = 'Drama' AND rating > 8)
#   )

query = (
    Movie.query()
    .where("status", "=", "available")
    .andWhere(lambda q: (
        q.where(lambda group1: (
            group1.where("genre", "=", "Comedy").andWhere("rating", ">", 7)
        )).orWhere(lambda group2: (
            group2.where("genre", "=", "Drama").andWhere("rating", ">", 8)
        ))
    ))
)
```

## `whereLike` and `whereILike`

To filter using `LIKE` and case-insensitive `ILIKE`, you can use the `whereLike` and `whereILike` methods.

### `whereLike`

This generates a `WHERE col LIKE 'pattern'` clause.

```python
# SELECT * FROM movies WHERE title LIKE 'The %'
Movie.query().whereLike("title", "The %")
```

### `whereILike`

This filters with case-insensitive matching. On Postgres it renders native `ILIKE`. On every other dialect it compiles to `LOWER(col) LIKE LOWER(pattern)`.

```python
# Postgres: SELECT * FROM "movies" WHERE "title" ILIKE 'the %'
# Others:   SELECT * FROM movies WHERE LOWER(title) LIKE LOWER('the %')
Movie.query().whereILike("title", "the %")
```

## `whereNull` and `whereNotNull`

To check for `NULL` or `NOT NULL` values, use `whereNull` and `whereNotNull`.

### `whereNull`

This generates a `WHERE col IS NULL` clause.

```python
# SELECT * FROM movies WHERE tagline IS NULL
Movie.query().whereNull("tagline")
```

### `whereNotNull`

This generates a `WHERE col IS NOT NULL` clause.

```python
# SELECT * FROM movies WHERE tagline IS NOT NULL
Movie.query().whereNotNull("tagline")
```

## `whereBetween` and `whereNotBetween`

To filter for values within a specific range, use the `whereBetween` and `whereNotBetween` methods.

### `whereBetween`

This generates a `WHERE col BETWEEN val1 AND val2` clause.

```python
# SELECT * FROM movies WHERE release_year BETWEEN 1990 AND 1999
Movie.query().whereBetween("release_year", 1990, 1999)

# You can also use `andWhereBetween` and `orWhereBetween`
#   SELECT *
#   FROM movies
#   WHERE genre = 'Action'
#     AND release_year BETWEEN 1990 AND 1999
Movie.query().where("genre", "=", "Action").andWhereBetween("release_year", 1990, 1999)
```

### `whereNotBetween`

This generates a `WHERE col NOT BETWEEN val1 AND val2` clause.

```python
# SELECT * FROM movies WHERE release_year NOT BETWEEN 1990 AND 1999
Movie.query().whereNotBetween("release_year", 1990, 1999)
```

## `whereExists` and `whereNotExists`

To filter based on the existence of records in a subquery, use the `whereExists` and `whereNotExists` methods. These methods accept a `QueryBuilder` instance or a callable that receives a `QueryBuilder` instance, allowing you to construct the subquery.

### `whereExists`

This generates a `WHERE EXISTS (...)` clause.

```python
from my_project import User, Post

# SELECT *
# FROM users
# WHERE EXISTS (
#   SELECT 1
#   FROM posts
#   WHERE posts.user_id = users.id
# )
User.query().whereExists(
    Post.query().select(QueryBuilder.raw("1")).where("posts.user_id", "=", QueryBuilder.raw("users.id"))
)

# Using a callable for the subquery
# SELECT *
# FROM users
# WHERE EXISTS (
#   SELECT 1
#   FROM posts
#   WHERE posts.user_id = users.id AND posts.status = 'published'
# )
User.query().whereExists(lambda q: (
    q.from_(Post)
     .select(QueryBuilder.raw("1"))
     .where("posts.user_id", "=", QueryBuilder.raw("users.id"))
     .andWhere("posts.status", "=", "published")
))
```
> **Note:** When referencing columns from the outer query within the subquery (e.g., `users.id`), use `QueryBuilder.raw()` to prevent the column name from being treated as a string literal.

### `whereNotExists`

This generates a `WHERE NOT EXISTS (...)` clause.

```python
from my_project import User, Post

# SELECT *
# FROM users
# WHERE NOT EXISTS (
#   SELECT 1
#   FROM posts
#   WHERE posts.user_id = users.id
# )
User.query().whereNotExists(
    Post.query().select(QueryBuilder.raw("1")).where("posts.user_id", "=", QueryBuilder.raw("users.id"))
)
```
