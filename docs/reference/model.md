---
layout: default
title: Model reference
---

Subclass `sustained.Model` to describe a table. The subclass holds the table's name, its columns, its relations, and the dialect and connection its queries use.

Guide: [Models](/models).

## Class attributes you declare

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tableName` | `str` | `None` | The table name. Required for column access, DDL, and queries. |
| `tableSchema` | `str` | `None` | Schema name, the middle segment of a qualified name. |
| `database` | `str` | `None` | Database name, the first segment of a qualified name. |
| `tableColumns` | `dict[str, ColumnDef]` | `None` | Typed column definitions. Drives `create_table_sql()` and migration generation. |
| `columns` | `tuple[str, ...]` | `None` | Declared column names. When set, access to any other name raises `AttributeError`. |
| `indexes` | `list[Index]` | `None` | Named indexes, created alongside the table. |
| `tableConstraints` | `list[Check or ForeignKey]` | `None` | Named table constraints, rendered into CREATE TABLE and kept in step by migration generation. |
| `tableOptions` | `TableOptions` | `None` | Storage clauses. Athena only; every other dialect raises `DialectError`. |
| `relationMappings` | `dict[str, RelationMapping]` | `{}` | Relations, keyed by the name you join or fetch by. |

Declaring `tableColumns` sets `columns` from the same keys, unless the class sets `columns` itself.

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
| `Model.column` | `str` | The fully qualified name: `database.schema.table.column`, skipping the parts the model does not set. |
| `instance.column` | `str` | The same qualified name, from an instance. |
| `Model.c.column` | `ColumnExpr` | A typed reference. Python operators on a `ColumnExpr` build `Predicate` objects. |

All three forms raise `AttributeError` when `tableName` is unset, when the name starts with `_`, or when the model declares `columns` and the name is not one of them.

## Queries and dialect

```python
Model.query() -> QueryBuilder
```
{: .sig #query}

A new builder on the model's table, using the model's dialect.

```python
Model.set_dialect(dialect)
```
{: .sig #set_dialect}

Sets the dialect for every query, DDL statement, and migration built from this class. Call it on `Model` to cover every model, or on a subclass to scope it to that subclass.

## Connections

```python
Model.bind(connection)
```
{: .sig #bind}

Attaches a DB-API 2.0 connection or a `ConnectionPool`. Binding on `Model` shares the connection with every model; binding on a subclass scopes it to that subclass.

```python
Model.unbind()
```
{: .sig #unbind}

Removes the binding.

```python
Model.bind_async(adapter)
```
{: .sig #bind_async}

Attaches an `AsyncAdapter`. `Model.unbind_async()` removes it again.

```python
Model.transaction(connection=None)
```
{: .sig #transaction}

A context that commits on success and rolls back on any exception. Nested blocks use savepoints, spelled per dialect; on DuckDB, which has none, a nested block raises `DialectError`. Raises `RuntimeError` when no connection resolves.

```python
Model.async_transaction(adapter=None)
```
{: .sig #async_transaction}

The async equivalent. Nested blocks use savepoints, spelled the way the model's dialect spells them.

The connection's parameter style must match the dialect's placeholder. See [Dialect support](/reference/dialects).

## DDL

```python
Model.create_table_sql(if_not_exists=False) -> str
```
{: .sig #create_table_sql}

The CREATE TABLE statement from `tableColumns` and `tableOptions`. Raises `ValueError` when the model sets no `tableColumns` or no `tableName`.

```python
Model.create_indexes_sql() -> list[str]
```
{: .sig #create_indexes_sql}

One CREATE INDEX statement per entry in `indexes`. Empty when `indexes` is unset.

```python
Model.create_table_statements(if_not_exists=False) -> list[str]
```
{: .sig #create_table_statements}

The CREATE TABLE statement plus the CREATE INDEX statements.

```python
Model.create_table(connection=None, if_not_exists=False)
```
{: .sig #create_table}

Executes the CREATE TABLE and CREATE INDEX statements.

```python
Model.drop_table_sql(if_exists=True) -> str
```
{: .sig #drop_table_sql}

The DROP TABLE statement. Raises `ValueError` when the model sets no `tableName`.

```python
Model.drop_table(connection=None, if_exists=True)
```
{: .sig #drop_table}

Executes the DROP TABLE statement.

Column types render per dialect. See [Schema types](/reference/schema).

## Instances

`run()` and `first()` return instances with one attribute per result column. Instances do not lazy load, do not track changes, and have no `save()` method.

```python
Model(**kwargs)
```
{: .sig #model}

Sets each keyword as an attribute.

```python
repr(instance)
```
{: .sig #repr}

`ClassName(key=value, ...)` over the instance's attributes.

## Relations

A `relationMappings` entry has three keys.

| Key | Value |
| --- | --- |
| `relation` | A `RelationType` member. |
| `modelClass` | The related model class, or its class name as a string. |
| `join` | How the two tables connect. |

The `RelationType` members are `BelongsToOneRelation`, `HasManyRelation`, `HasOneRelation`, and `ManyToManyRelation`.

A direct join mapping is `{'from': 'a.id', 'to': 'b.a_id'}`. Qualify both sides as `table.column`.

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

String names in `modelClass` resolve through the model registry: every subclass with a `tableName` registers itself under its class name when the class is defined. Import the related class before you build the query, or resolution raises `ValueError`.

## Registry functions

These live in `sustained.model`.

```python
get_registered_model(name) -> type[Model] | None
```
{: .sig #get_registered_model}

Looks up a model by class name. Raises `ValueError` when two model classes share the name, because the registry keeps neither of them under it. The registry never drops an entry, so a name stays taken for the life of the process.

```python
resolve_model_reference(reference, context_module=None) -> type[Model]
```
{: .sig #resolve_model_reference}

Returns a class unchanged, or resolves a string through the registry, then through `context_module`. Raises `ValueError` when neither one finds the class. A name two model classes share resolves through `context_module` only, and raises when that module does not define it.

## Building a model at runtime

```python
create_model(name, table_name, mappings=None, table_schema=None, database=None, columns=None)
```
{: .sig #create_model}

`create_model` returns a new `Model` subclass. `mappings` becomes `relationMappings`, and `columns` sets the strict column tuple. The result behaves like a class you wrote by hand, and registers itself the same way.

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
