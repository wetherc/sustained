---
layout: default
title: Building Queries
---

Once you have defined your models, you can start building queries using the `QueryBuilder`.

[<-- Back to Index](./index)

## Starting a Query

All queries begin with the `query()` class method on a `Model` subclass. This returns a new `QueryBuilder` instance, which you can use to chain methods.

```python
from my_project import User

# Get a query builder for the User model
query_builder = User.query()
```

## Selecting Columns

The `select()` method allows you to specify which columns your query should return.

### Selecting Specific Columns

Pass any number of column name strings to `select()`.

```python
# Builds: SELECT id, name, email FROM users
query = User.query().select('id', 'name', 'email')
```

If `select()` is never called, the query will default to selecting all columns (`SELECT *`).

### Using Column Name Access

For clarity and to avoid ambiguity in joins, it's often a good idea to use the model's column access feature to get fully-qualified column names.

```python
user = User()
person = Person()

# Builds: SELECT users.id, persons.firstName FROM users...
query = User.query().select(user.id, person.firstName)
```

## Offsetting Results

The `offset()` method allows you to skip a specified number of rows in the query result. This is useful for pagination.

```python
# Builds: SELECT * FROM users OFFSET 10
query = User.query().select('*').offset(10)
```

The `offset()` method can only be called once per query and requires an integer value.

## Limiting Results

Sustained provides two ways to limit the number of rows returned by a query: `limit()` for most SQL databases and `top()` for SQL Server-style queries.

### `limit()`

The `limit()` method adds a `LIMIT` clause to the end of your query. This is the standard way to limit results in databases like PostgreSQL, MySQL, and SQLite.

```python
# Builds: SELECT * FROM users LIMIT 10
query = User.query().select('*').limit(10)

# You can also combine it with offset for pagination
# Builds: SELECT * FROM users LIMIT 10 OFFSET 20
paginated_query = User.query().select('*').limit(10).offset(20)
```

### `top()`

The `top()` method uses SQL Server's `TOP N` syntax, which places the limiter at the beginning of the `SELECT` statement.

```python
# Builds: SELECT TOP 10 * FROM users
query = User.query().select('*').top(10)
```

### Usage Notes

-   The `limit()` and `top()` methods are mutually exclusive. Using both in the same query will result in a `ValueError`.
-   Both methods can only be called once per query and require an integer value.

## Ordering Results

The `orderBy()` method allows you to sort the result set of your query.

### `orderBy()`

You can specify one or more columns to sort by, along with an optional direction (`'asc'` for ascending or `'desc'` for descending). If no direction is provided, `'asc'` is assumed.

```python
# Builds: SELECT * FROM users ORDER BY name ASC
query = User.query().select('*').orderBy('name')

# Builds: SELECT * FROM users ORDER BY age DESC
query = User.query().select('*').orderBy('age', 'desc')

# You can chain multiple orderBy calls to sort by multiple columns
# Builds: SELECT * FROM users ORDER BY name ASC, age DESC
query = User.query().select('*').orderBy('name').orderBy('age', 'desc')
```

### Behavior with Other Clauses

-   **`LIMIT` and `OFFSET`**: The `ORDER BY` clause is applied before `LIMIT` and `OFFSET`. This ensures that the correct rows are selected for limiting and offsetting after the sorting has occurred.
-   **`UNION`**: When used with a `UNION`, the `ORDER BY` clause applies to the entire result set of the combined queries, not to individual `SELECT` statements within the `UNION`.

```python
# Builds: SELECT * FROM users ORDER BY name DESC LIMIT 10 OFFSET 5
query = User.query().select('*').orderBy('name', 'desc').limit(10).offset(5)
```

## Common Table Expressions (CTEs)

You can add CTEs to your query using the `.with_()` method. Note the trailing underscore, which is necessary to avoid conflicting with Python's `with` keyword.

The `.with_()` method takes two arguments:
1.  An alias (string) for the CTE.
2.  A `QueryBuilder` instance for the CTE's subquery.

```python
# Build a CTE for active users
active_users_cte = User.query().select('id').where('status', '=', 'active')

# Use the CTE in a main query to get their posts
# (Assumes a Post model exists)
posts_query = (
    Post.query()
    .with_('active_users', active_users_cte)
    .join('active_users', 'posts.user_id', '=', 'active_users.id')
    .select('posts.title')
)
print(posts_query)

# Builds:
# WITH active_users AS (
#   SELECT id
#   FROM users
#   WHERE status = 'active'
# )
# SELECT posts.title
# FROM posts
# JOIN active_users
#   ON posts.user_id = active_users.id
```

## Combining Queries with UNION

You can combine multiple queries into a single result set using `UNION` and `UNION ALL`.

### `union()` and `unionAll()`

Use the `union()` and `unionAll()` methods to combine a query with one or more other queries. These methods accept any number of `QueryBuilder` instances as arguments.

-   `union()`: Combines the results and removes duplicate rows.
-   `unionAll()`: Combines the results and includes all rows, including duplicates.

```python
# Assume User and Customer models exist and have compatible columns
active_users = User.query().select('id', 'name').where('active', '=', True)
pending_users = User.query().select('id', 'name').where('status', '=', 'pending')

all_users = active_users.union(pending_users)

print(all_users)
# Builds:
# (SELECT id, name FROM users WHERE active = True) UNION (SELECT id, name FROM users WHERE status = 'pending')
```

### Behavior with Other Clauses

-   **`OFFSET`**: When used with a `UNION`, the `offset()` method applies to the entire result set of the combined queries.
-   **`WITH` (CTEs)**: If any of the queries in a `UNION` chain have Common Table Expressions, they will all be "hoisted" to the top of the final query. Sustained handles this automatically.

```python
# This query will offset the result of the entire UNION
final_query = active_users.union(pending_users).offset(50)
```

## Retrieving the SQL

The `QueryBuilder` does not execute the query. It only builds the SQL string. To get the final SQL, simply convert the builder instance to a string.

```python
query = User.query().select('name').where('id', '=', 1)

# Get the SQL string
sql_string = str(query)

print(sql_string)
# "SELECT name FROM users WHERE id = 1"
```

This design allows you to use Sustained with any database driver. You build the query with Sustained, and then execute the resulting SQL string with your preferred library (e.g., `psycopg2`, `pyodbc`, etc.).
