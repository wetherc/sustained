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

For clarity and to avoid ambiguity in joins, it's often a good idea to use the model's column access feature to get fully-qualified column names. Column access works directly on the model class.

```python
# Builds: SELECT users.id, persons.firstName FROM users...
query = User.query().select(User.id, Person.firstName)
```

You can also alias a column with the `'column AS alias'` shorthand. Both halves are quoted correctly for the active dialect.

```python
# Builds: SELECT name AS display_name FROM users
query = User.query().select('name AS display_name')
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

#### Generic Functions with `select_func()`

For any SQL function that doesn't have a dedicated fluent method, you can use the generic `.select_func()` method. This is the most flexible way to add function calls to your query.

String arguments are column references. To pass a string literal, wrap it in `Literal`. To pass a raw SQL fragment, use `Column`.

```python
from sustained import Literal

# SELECT COALESCE(nickname, first_name, 'N/A') AS display_name FROM users
query = User.query().select_func(
    'COALESCE',
    'nickname',
    'first_name',
    Literal('N/A'),
    alias='display_name'
)
```

A string argument that is not a plain column name raises a `ValueError` when the query renders. This protects you from a literal that silently becomes a column, or the reverse.

Every registered function is also available as a method of its own name, so these two lines build the same query:

```python
User.query().select_func('COALESCE', 'nickname', 'first_name', alias='d')
User.query().coalesce('nickname', 'first_name', alias='d')
```

The shortcuts include `lower()`, `upper()`, `coalesce()`, `concat()`, `substring()`, `trim()`, `length()`, `round()`, `abs()`, `ceiling()`, `floor()`, `mod()`, `now()`, and `getdate()`.

Some functions translate per dialect: `now()` renders as `GETDATE()` on MSSQL, `getdate()` renders as `NOW()` on Postgres, and `length()` renders as `LEN()` on MSSQL. You write the name you know, and the dialect gets the spelling it needs. A registered function raises `DialectError` on a dialect that has no spelling for it, so `now()` fails on the default dialect at build time instead of in the database.

##### Dialect Validation

A key feature of `.select_func()` is its runtime dialect validation. The method checks the function name against an internal `FunctionRegistry`.

*   **If a function is registered:** The query builder will check if it is supported by the currently configured dialect. If it is not, a `DialectError` will be raised immediately. This prevents you from sending an invalid query to your database.
*   **If a function is not registered:** The query builder will allow it to pass through without validation. This provides the flexibility to use any custom or obscure database-specific functions at your own risk.

```python
# Assume the dialect for the User model is set to MSSQL
User.set_dialect(Dialects.MSSQL)

# This will raise a DialectError, because 'STRING_AGG' is registered
# but not supported by the MSSQL dialect.
with self.assertRaises(DialectError):
    User.query().select_func('STRING_AGG', 'name')

# This will succeed, because 'SOME_MSSQL_ONLY_FUNCTION' is not
# in the registry and is allowed to pass through.
query = User.query().select_func('SOME_MSSQL_ONLY_FUNCTION', 'column')
```

##### Registered Common Functions

While `.select_func()` can be used for any function, the `FunctionRegistry` includes a set of common scalar functions that are validated across all supported dialects:
- `LOWER`
- `UPPER`
- `COALESCE`
- `CONCAT`
- `SUBSTRING`
- `TRIM`
- `LENGTH`
- `ROUND`
- `ABS`
- `CEILING`
- `FLOOR`
- `MOD`

#### Subqueries in Select

You can use a `Subquery` object to embed a subquery directly into your `SELECT` list.

```python
from sustained.expressions import Subquery

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
-   Both methods can only be called once per query and require a non-negative integer value.
-   `top()` raises a `DialectError` on dialects other than MSSQL. Use `limit()` there instead.
-   On MSSQL, `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL only allows after an `ORDER BY`. The query raises a `DialectError` if you use them without `orderBy()`.
-   On Presto, `OFFSET` renders before `LIMIT`, as the engine requires.

### Pagination with `page()`

The `page()` method applies `LIMIT` and `OFFSET` from a zero-based page number and a page size.

```python
# Builds: SELECT * FROM users LIMIT 25 OFFSET 50
query = User.query().select('*').page(2, 25)
```

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

### Recursive CTEs

Pass `recursive=True` for a self-referencing CTE. The WITH clause renders as `WITH RECURSIVE`, except on MSSQL, where T-SQL spells recursive CTEs with plain `WITH`.

```python
tree = Employee.query().select('id', 'manager_id')  # anchor plus recursion via raw()
query = Employee.query().with_('tree', tree, recursive=True).from_('tree')
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
# (SELECT id, name FROM users WHERE active = TRUE) UNION (SELECT id, name FROM users WHERE status = 'pending')
```

### Behavior with Other Clauses

-   **`ORDER BY`, `LIMIT`, `OFFSET` on the outer query**: These apply to the entire result set of the combined queries.
-   **`ORDER BY` and `LIMIT` on a member query**: These render inside that member's parentheses, so each member keeps its own row cap and ordering.
-   **`WITH` (CTEs)**: If any of the queries in a `UNION` chain have Common Table Expressions, they will all be "hoisted" to the top of the final query. Sustained handles this automatically. Two different CTEs with the same alias raise a `ValueError`.

```python
# This query will offset the result of the entire UNION
final_query = active_users.union(pending_users).offset(50)
```

### INTERSECT and EXCEPT

`intersect()` keeps only rows present in every query. `except_()` removes rows that appear in the given queries. The trailing underscore avoids the Python `except` keyword. Both work like `union()`.

## Analyst Clauses

*   **`distinctOn(*columns)`**: Postgres/DuckDB `SELECT DISTINCT ON (...)`, which keeps the first row per group; pair it with `orderBy()` on the same leading columns. Other dialects raise `DialectError`.
*   **`groupByRollup(*columns)` / `groupByCube(*columns)` / `groupByGroupingSets(*tuples)`**: subtotal and multi-grain aggregation forms of GROUP BY.
*   **`qualify(condition)`**: filters on window function results without a wrapping subquery. Takes a `Predicate` or a raw string. Supported on DuckDB.
*   **`for_update(skip_locked=False, nowait=False)`**: row locking on Postgres; rejected with unions and on dialects without it.

## Counting and Keyset Pagination

`total()` runs `SELECT COUNT(*)` over the query with ORDER BY, LIMIT, and OFFSET stripped, and returns the count without changing the query. `cursor_page(column, page_size, after=None)` applies keyset pagination: it orders by the column, filters rows greater than the last seen value, and limits to the page size. On large tables this avoids the scan cost that grows with OFFSET.

```python
first_page = User.query().cursor_page('id', 100).run()
second_page = User.query().cursor_page('id', 100, after=first_page[-1].id).run()
```

## Inspecting Plans

`explain()` runs the dialect's EXPLAIN on the query and returns the plan rows. `explain(analyze=True)` uses EXPLAIN ANALYZE, which actually executes the statement. MSSQL raises because T-SQL has no EXPLAIN.

## SQL Dialects

Sustained supports generating SQL for different database dialects. This allows you to take advantage of dialect-specific features and syntax. By default, Sustained generates standard ANSI SQL.

Currently, the following dialects are supported:

*   **`Dialects.DEFAULT`**: Standard ANSI SQL (Default)
*   **`Dialects.PRESTO`**: SQL dialect for the Presto query engine.
*   **`Dialects.ATHENA`**: SQL dialect for AWS Athena (Trino-based). Inherits Presto's query behavior with Athena's DDL, `%s` placeholders for pyathena, and MERGE upserts on Iceberg tables.
*   **`Dialects.MSSQL`**: SQL dialect for Microsoft SQL Server.
*   **`Dialects.POSTGRES`**: SQL dialect for PostgreSQL.
*   **`Dialects.DUCKDB`**: SQL dialect for DuckDB.

### Setting the Dialect

You can set the dialect on a model class using the `set_dialect()` class method. All queries built from that model will then use the specified dialect for SQL generation.

```python
from sustained import Model
from sustained.dialects import Dialects

class User(Model):
    tableName = "users"

# Set the dialect for the User model to Presto
User.set_dialect(Dialects.PRESTO)

# This query will now be compiled using the Presto dialect
query = User.query().select("name").where("id", "=", 1)

# The resulting SQL will use Presto-specific syntax if applicable
# e.g., SELECT "name" FROM "users" WHERE "id" = 1
sql_string = str(query)

print(sql_string)
```

This is useful if your entire application targets a single database type. You can set the dialect for each of your models once, during application startup.

## Reusing Queries with `clone()`

Builders change in place: each chained call adds to the same query. Use `clone()` to branch from a shared base query without changing the original.

```python
base = User.query().where('active', '=', True)

admins = base.clone().where('role', '=', 'admin')
guests = base.clone().where('role', '=', 'guest')
```

## Method Naming

The canonical method names use camelCase, which matches Objection.js: `orderBy`, `groupBy`, `whereIn`, `unionAll`, `leftJoin`, and so on. Every camelCase method also accepts its snake_case spelling: `order_by`, `group_by`, `where_in`, `union_all`, `left_join`.

## Retrieving the SQL

To debug or log a query, convert the builder to a string. Values render inline as SQL literals.

```python
query = User.query().select('name').where('id', '=', 1)

print(str(query))
# "SELECT name FROM users WHERE id = 1"
```

To execute a query, use `to_sql()` instead. It returns the SQL with placeholders and the parameters as a separate tuple, in the order they appear in the SQL. Pass both to any DB-API cursor. This keeps user values out of the SQL text.

```python
sql, params = User.query().select('name').where('id', '=', 1).to_sql()

# sql:    "SELECT name FROM users WHERE id = ?"
# params: (1,)
cursor.execute(sql, params)
```

The placeholder style follows the dialect: `?` by default and for MSSQL, `%s` for Postgres and Athena.

Sustained can also execute queries for you and hydrate the results into model instances. See [Executing Queries](./executing) for `Model.bind()`, `run()`, `first()`, and eager loading, and for the `insert()`, `update()`, and `delete()` statement builders.
