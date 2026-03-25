# Building Queries

Once you have defined your models, you can start building queries using the `QueryBuilder`.

[<-- Back to Index](./index.md)

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

## Joining Tables

For simple joins where you don't have or need a pre-defined relation on your model, you can use the raw join methods. These methods are generated dynamically for each join type (`join`, `innerJoin`, `leftJoin`, `rightJoin`, etc.).

They accept four arguments:
1.  The table to join to.
2.  The first column for the `ON` condition.
3.  The operator for the `ON` condition.
4.  The second column for the `ON` condition.

```python
# Builds: SELECT persons.*, animals.name FROM persons LEFT JOIN animals ON persons.id = animals.ownerId
query = Person.query().leftJoin('animals', 'persons.id', '=', 'animals.ownerId')
```

### Complex Joins with Lambdas

For joins that require multiple or complex `ON` conditions, you can pass a lambda function as the second argument to any of the `join` methods. This lambda receives a `JoinBuilder` object that you can use to construct the join conditions.

The `JoinBuilder` has the following methods:
*   `on(col1, op, col2)`: Adds the initial `ON` condition.
*   `andOn(col1, op, col2)`: Adds an `AND` condition to the join.
*   `orOn(col1, op, col2)`: Adds an `OR` condition to the join.

```python
# Builds:
# SELECT * FROM users
# JOIN accounts ON accounts.id = users.account_id AND accounts.enabled = 1 OR accounts.owner_id = users.id
query = User.query().join(
    'accounts',
    lambda j: j.on('accounts.id', '=', 'users.account_id')
    .andOn('accounts.enabled', '=', '1')
    .orOn('accounts.owner_id', '=', 'users.id'),
)
```

For more complex joins based on your data model, see the [Relations documentation](./relations.md).

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

# Builds:
# WITH active_users AS (SELECT id FROM users WHERE status = 'active')
# SELECT posts.title FROM posts JOIN active_users ON posts.user_id = active_users.id
print(posts_query)
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

This design allows you to use Objection.py with any database driver. You build the query with Objection.py, and then execute the resulting SQL string with your preferred library (e.g., `psycopg2`, `pyodbc`, etc.).
