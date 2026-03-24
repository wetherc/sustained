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
