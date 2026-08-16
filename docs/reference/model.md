---
layout: default
title: Model reference
---

`sustained.Model` is the base class for every table. A subclass carries the
table's name, its columns, its relations, and the dialect and connection its
queries use.

Guide: [Models](/models).

## Class attributes you declare

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tableName` | `str` | `None` | The table name. Required for column access, DDL, and queries. |
| `tableSchema` | `str` | `None` | Schema name, the middle segment of a qualified name. |
| `database` | `str` | `None` | Database name, the first segment. |
| `tableColumns` | `dict[str, ColumnDef]` | `None` | Typed column definitions. Drives `create_table_sql()` and migration generation. |
| `columns` | `tuple[str, ...]` | `None` | Declared column names. When set, access to any other name raises `AttributeError`. |
| `indexes` | `list[Index]` | `None` | Named indexes, created alongside the table. |
| `tableOptions` | `TableOptions` | `None` | Storage clauses. Athena only; every other dialect raises `DialectError`. |
| `relationMappings` | `dict[str, RelationMapping]` | `{}` | Relations, keyed by the name you join or fetch by. |

Declaring `tableColumns` sets `columns` for you, from its keys, unless the
class sets `columns` itself. So a typed model gets strict column names without
asking.

```python
from sustained import Model
from sustained.schema import Integer, String

class Venue(Model):
    tableName = 'venues'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'name': String(120, nullable=False),
    }

Venue.name        # 'venues.name'
Venue.nmae        # AttributeError, listing the declared columns
```

## Column access

| Form | Returns | Notes |
| --- | --- | --- |
| `Model.column` | `str` | The fully qualified name: `database.schema.table.column`, skipping unset parts. |
| `instance.column` | `str` | The same, from an instance. |
| `Model.c.column` | `ColumnExpr` | A typed reference. Python operators on it build `Predicate` objects. |

All three raise `AttributeError` when `tableName` is unset, when the name
starts with `_`, or when `columns` is declared and the name is not in it.

## Queries and dialect

| Signature | Returns | Description |
| --- | --- | --- |
| `Model.query()` | `QueryBuilder` | A new builder on the model's table, using the model's dialect. |
| `Model.set_dialect(dialect)` | `None` | Sets the dialect for every query, DDL statement, and migration built from this class. Call it on `Model` to cover everything, or on a subclass to scope it. |

## Connections

| Signature | Description |
| --- | --- |
| `Model.bind(connection)` | Attaches a DB-API 2.0 connection or a `ConnectionPool`. Binding on `Model` shares it with every model; binding on a subclass scopes it. |
| `Model.unbind()` | Removes the binding. |
| `Model.bind_async(adapter)` | Attaches an `AsyncAdapter`. |
| `Model.unbind_async()` | Removes it. |
| `Model.transaction(connection=None)` | A context that commits on success and rolls back on any exception. Nested blocks use savepoints. Raises `RuntimeError` when no connection resolves. |
| `Model.async_transaction(adapter=None)` | The async equivalent. Does not nest: a second block raises `RuntimeError`. |

The connection's parameter style must match the dialect's placeholder. See
[Dialect support](/reference/dialects).

## DDL

| Signature | Returns | Description |
| --- | --- | --- |
| `Model.create_table_sql(if_not_exists=False)` | `str` | The CREATE TABLE statement from `tableColumns` and `tableOptions`. Raises `ValueError` without `tableColumns` or without `tableName`. |
| `Model.create_indexes_sql()` | `list[str]` | One CREATE INDEX per entry in `indexes`. Empty when `indexes` is unset. |
| `Model.create_table_statements(if_not_exists=False)` | `list[str]` | The CREATE TABLE plus the CREATE INDEX statements. |
| `Model.create_table(connection=None, if_not_exists=False)` | `None` | Executes all of them. |
| `Model.drop_table_sql(if_exists=True)` | `str` | The DROP TABLE statement. Raises `ValueError` without `tableName`. |
| `Model.drop_table(connection=None, if_exists=True)` | `None` | Executes it. |

Types render per dialect. See [Schema types](/reference/schema).

## Instances

`run()` and `first()` return instances with one attribute per result column.
An instance is a plain object: no lazy loading, no dirty tracking, no `save()`.

| Signature | Description |
| --- | --- |
| `Model(**kwargs)` | Sets each keyword as an attribute. |
| `repr(instance)` | `ClassName(key=value, ...)` over the instance's attributes. |

## Relations

A `relationMappings` entry has three keys.

| Key | Value |
| --- | --- |
| `relation` | A `RelationType` member. |
| `modelClass` | The related model class, or its class name as a string. |
| `join` | How the tables connect. |

`RelationType` members: `BelongsToOneRelation`, `HasManyRelation`,
`HasOneRelation`, `ManyToManyRelation`.

A direct join mapping is `{'from': 'a.id', 'to': 'b.a_id'}`. Both sides must
be qualified as `table.column`.

A through mapping adds the link table:

```python
'join': {
    'from': 'artists.id',
    'through': {
        'from': {'table': 'show_artists', 'key': 'artist_id'},
        'to': {'table': 'show_artists', 'key': 'show_id'},
    },
    'to': 'shows.id',
}
```

String names in `modelClass` resolve through the model registry: every
subclass with a `tableName` registers itself under its class name when the
class is defined. The class must be imported before the query is built, or
resolution raises `ValueError`.

## Registry functions

In `sustained.model`.

| Signature | Returns | Description |
| --- | --- | --- |
| `get_registered_model(name)` | `type[Model]` or `None` | Looks up a model by class name. |
| `resolve_model_reference(reference, context_module=None)` | `type[Model]` | Returns a class unchanged, or resolves a string through the registry, then through `context_module`. Raises `ValueError` when neither finds it. |

## Building a model at runtime

```python
create_model(name, table_name, mappings=None, table_schema=None,
             database=None, columns=None)
```

Returns a new `Model` subclass. `mappings` becomes `relationMappings`;
`columns` sets the strict column tuple. The result behaves like a class you
wrote by hand, registry included.

```python
from sustained import create_model, RelationType

Venue = create_model('Venue', 'venues')
Show = create_model(
    'Show', 'shows',
    mappings={
        'venue': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': Venue,
            'join': {'from': 'shows.venue_id', 'to': 'venues.id'},
        },
    },
)
```
