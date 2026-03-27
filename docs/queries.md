---
layout: default
title: Building Queries
---

Once you have defined your models, you can start building queries using the `QueryBuilder`.



## Starting a Query

All queries begin with the `query()` class method on a `Model` subclass. This returns a new `QueryBuilder` instance, which you can use to chain methods.

```python
from my_project import User

# Get a query builder for the User model
query_builder = User.query()
```

## FROM Clause

The `from_()` method allows you to explicitly define the table or subquery that the query will operate on, overriding the default table name derived from the model.

### Specifying a Raw Table Name

You can pass a string to `from_()` to use a custom table name or to alias the model's default table.

```python
from my_project import User

# SELECT * FROM custom_users_table AS cu
query = User.query().from_('custom_users_table', 'cu')

# Overriding the default table name
# SELECT * FROM users_archive
query = User.query().from_('users_archive')
```

### Using a Subquery in FROM

You can also use a `QueryBuilder` instance as the source for your `FROM` clause. When using a subquery, an alias is required.

```python
from my_project import Movie

# Subquery to find top-rated movies
top_movies_subquery = Movie.query().where('rating', '>', 8).select('id', 'title')

# Main query using the subquery as the FROM source
# SELECT * FROM (SELECT id, title FROM movies WHERE rating > 8) AS top_rated_films
query = Movie.query().from_(top_movies_subquery, 'top_rated_films').select('*')
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

### Distinct

You can add a `DISTINCT` keyword to your query to retrieve only unique rows.

```python
# Builds: SELECT DISTINCT country FROM users
query = User.query().distinct().select('country')
```

### Advanced Selections

The `select` method is not limited to simple column names. You can pass in various expression objects to perform more complex queries. The `QueryBuilder` also provides several fluent API methods to make this even easier.

#### Aggregates: `count()`, `sum()`, etc.

You can perform aggregate calculations using the `AggregateExpression` class or the corresponding fluent methods.

**Using Fluent Methods:**

The easiest way is to use methods like `count()`, `sum()`, `avg()`, `min()`, and `max()`.

```python
# SELECT COUNT(*) FROM users
query = User.query().count()

# SELECT COUNT(id) AS total FROM users
query = User.query().count('id', alias='total')

# SELECT SUM(amount) AS total_amount FROM orders
query = Order.query().sum('amount', alias='total_amount')

# SELECT AVG(price) AS average_price FROM products
query = Product.query().avg('price', alias='average_price')

# SELECT MIN(age) AS youngest FROM users
query = User.query().min('age', alias='youngest')

# SELECT MAX(age) AS oldest FROM users
query = User.query().max('age', alias='oldest')
```

**Using Expression Classes:**

You can also construct `AggregateExpression` objects manually and pass them to `select()`. This is useful for aggregates that don't have a dedicated fluent method.

```python
from sustained.expressions import AggregateExpression

# SELECT STRING_AGG(name, ', ') FROM users
query = User.query().select(AggregateExpression('STRING_AGG', 'name, \', \''))
```

#### Window Functions

Window functions can be created using the `select_window()` method or by constructing a `WindowExpression`.

```python
# SELECT
#   ROW_NUMBER() OVER (
#     PARTITION BY department
#     ORDER BY hire_date
#   ) AS seniority
# FROM employees
query = Employee.query().select_window(
    'ROW_NUMBER',
    'seniority',
    partition_by=['department'],
    order_by=['hire_date']
)
```

#### CASE Expressions

You can build `CASE` statements using the `select_case()` method. To distinguish between string literals and column names in the results, wrap column names in the `Column` object.

```python
from sustained.expressions import Column

# SELECT
#   CASE
#     WHEN score > 90 THEN 'Expert'
#     WHEN score > 50 THEN 'Intermediate'
#     ELSE 'Beginner'
#   END AS level
# FROM users
query = User.query().select_case(
    'level',
    'Beginner',
    when_clauses=[
        ('score > 90', 'Expert'),
        ('score > 50', 'Intermediate'),
    ]
)

# Use Column() for non-literal results
# SELECT
#   CASE
#     WHEN is_active = 1 THEN last_login_date
#     ELSE account_deactivated_date
#   END AS last_account_activity
# FROM users
query = User.query().select_case(
    'last_account_activity',
    Column('account_deactivated_date'),
    when_clauses=[
        ('is_active = 1', Column('last_login_date')),
    ]
)
```

#### Generic Functions

For any other SQL function, you can use the `Func` expression class.

```python
from sustained import Func, Column

# SELECT COALESCE(nickname, first_name) AS display_name FROM users
query = User.query().select(
    Func('COALESCE', Column('nickname'), Column('first_name'), alias='display_name')
)
```

#### Subqueries in Select

You can use a `Subquery` object to embed a subquery directly into your `SELECT` list.

```python
from sustained import Subquery

# SELECT
#   id,
#   (SELECT COUNT(*) FROM posts WHERE posts.user_id = users.id) AS post_count
# FROM users
post_count_subquery = Post.query().count().where('user_id', '=', Column('users.id'))

query = User.query().select(
    'id',
    Subquery(post_count_subquery, 'post_count')
)
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
