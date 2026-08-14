---
layout: default
title: Executing Queries
---

Sustained can execute the queries it builds. It works with any DB-API 2.0 connection, such as `sqlite3`, `psycopg`, or `pyodbc`. Every statement runs parameterized: user values travel as parameters, never as text inside the SQL.

The connection's parameter style must match the dialect. The default and MSSQL dialects use `?` (qmark). The Postgres dialect uses `%s` (format).

## Binding a Connection

Bind a connection once with `Model.bind()`. Every query on that model can then run without passing the connection each time.

```python
import sqlite3
from sustained import Model

class User(Model):
    tableName = 'users'

conn = sqlite3.connect('app.db')
User.bind(conn)
```

Bind on `Model` itself to share one connection across all models. Bind on a subclass to scope it. Call `Model.unbind()` to remove a binding. You can also pass a connection directly to `run()` or `first()`, which overrides any binding.

## Running SELECT Queries

`run()` executes the query and hydrates each row into a model instance. Column names come from the cursor description.

```python
users = User.query().where('active', '=', True).orderBy('name').run()

for user in users:
    print(user.name)
```

`first()` runs the query with `LIMIT 1` and returns one instance, or `None` when there is no match. The original query is not changed.

```python
user = User.query().where('email', '=', 'ada@example.com').first()
```

## Writing Data

`insert()`, `update()`, and `delete()` turn the builder into a write statement. They use the same `where()` methods as SELECT and the same parameterized rendering.

```python
# INSERT INTO users (name, email) VALUES (?, ?)
User.query().insert({'name': 'Ada', 'email': 'ada@example.com'}).run()

# Multi-row insert. All rows must have the same columns.
User.query().insert([
    {'name': 'Ada'},
    {'name': 'Grace'},
]).run()

# UPDATE users SET active = ? WHERE id = ?
User.query().update({'active': False}).where('id', '=', 1).run()

# DELETE FROM users WHERE active = ?
User.query().delete().where('active', '=', False).run()
```

Write statements commit after they run and return the affected row count.

### Safety Rule for UPDATE and DELETE

An `update()` or `delete()` without a `where()` clause raises a `ValueError`, because an unfiltered write usually means a missing filter. To write every row on purpose, add an always-true raw predicate:

```python
User.query().update({'active': True}).where(QueryBuilder.raw('1'), '=', 1).run()
```

### RETURNING

`returning()` adds a `RETURNING` clause on dialects that support it. The statement then returns a list of dicts instead of a row count. MSSQL and Presto raise a `DialectError`.

```python
rows = User.query().insert({'name': 'Ada'}).returning('id').run()
# [{'id': 42}]
```

## Eager Loading Relations

`withGraphFetched()` loads relations from `relationMappings` when the query runs. Each relation costs one extra query. `HasManyRelation` attaches a list to each instance. The to-one relation types attach a single instance or `None`.

```python
owners = Owner.query().withGraphFetched('pets').run()

for owner in owners:
    for pet in owner.pets:
        print(owner.name, pet.name)
```

Eager loading needs the join key columns in both result sets, so keep them in your `select()` or select all columns. Through relations (`ManyToManyRelation`) are not supported yet and raise `NotImplementedError`.
