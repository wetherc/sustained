---
layout: default
title: Schema types reference
---

Everything in `sustained.schema`. These objects are what a model's `tableColumns`, `indexes`, and `tableOptions` hold, together with the renderers that turn them into DDL.

Guide: [Schema and Migrations](/schema).

## Column types

Each type is a factory that returns a `ColumnDef`. Every factory accepts the full `ColumnDef` option set as keyword arguments.

| Signature | Logical type |
| --- | --- |
| `Integer(**options)` | `INTEGER` |
| `BigInteger(**options)` | `BIGINT` |
| `String(length=255, **options)` | `VARCHAR(length)` |
| `Text(**options)` | `TEXT` |
| `Boolean(**options)` | `BOOLEAN` |
| `Float(**options)` | `FLOAT` |
| `Numeric(precision=18, scale=6, **options)` | `NUMERIC(precision, scale)` |
| `Date(**options)` | `DATE` |
| `Timestamp(**options)` | `TIMESTAMP` |
| `Binary(**options)` | `BINARY` |
| `Json(**options)` | `JSON` |
| `Enum(*values, name, **options)` | `ENUM` (see [`Enum`](#enum)) |

### How types render per dialect

| Logical | Default, Presto | Postgres | MySQL | MSSQL | Athena | DuckDB |
| --- | --- | --- | --- | --- | --- | --- |
| INTEGER | `INTEGER` | `INTEGER` | `INT` | `INTEGER` | `INT` | `INTEGER` |
| BIGINT | `BIGINT` | `BIGINT` | `BIGINT` | `BIGINT` | `BIGINT` | `BIGINT` |
| VARCHAR | `VARCHAR` | `VARCHAR` | `VARCHAR` | `NVARCHAR` | `VARCHAR`, or `STRING` with no length | `VARCHAR` |
| TEXT | `TEXT` | `TEXT` | `TEXT` | `NVARCHAR(MAX)` | `STRING` | `TEXT` |
| BOOLEAN | `BOOLEAN` | `BOOLEAN` | `TINYINT(1)` | `BIT` | `BOOLEAN` | `BOOLEAN` |
| FLOAT | `DOUBLE PRECISION` | `DOUBLE PRECISION` | `DOUBLE` | `FLOAT` | `DOUBLE` | `DOUBLE PRECISION` |
| NUMERIC | `NUMERIC` | `NUMERIC` | `DECIMAL` | `NUMERIC` | `DECIMAL` | `NUMERIC` |
| DATE | `DATE` | `DATE` | `DATE` | `DATE` | `DATE` | `DATE` |
| TIMESTAMP | `TIMESTAMP` | `TIMESTAMP` | `DATETIME` | `DATETIME2` | `TIMESTAMP` | `TIMESTAMP` |
| BINARY | `BLOB`, or `VARBINARY` on Presto | `BYTEA` | `BLOB` | `VARBINARY(MAX)` | `BINARY` | `BLOB` |
| JSON | `JSON` | `JSONB` | `JSON` | `NVARCHAR(MAX)` | `STRING` | `JSON` |
| ENUM | `VARCHAR(n)` + CHECK | the named type | `ENUM(...)` | `NVARCHAR(n)` + CHECK | `DialectError` | the named type |

An ENUM column renders per the dialect's enum strategy. Postgres and DuckDB reference a named type created with `CREATE TYPE`. MySQL writes the value list inline. The default dialect and MSSQL render a VARCHAR sized to the longest value, held to the list by a CHECK constraint named `ck_<table>_<column>_enum`. Presto raises like Athena. [SQL Dialects](/dialects#enum-columns) has the details.

MySQL spells `BOOLEAN` as `TINYINT(1)` because its catalog reports the underlying type rather than the synonym. It spells `TIMESTAMP` as `DATETIME` because a MySQL `TIMESTAMP` column is four bytes, stops in 2038, and converts time zones, while `Timestamp()` describes a plain wall clock.

## `ColumnDef`

```python
ColumnDef(type_name, *, length=None, precision=None, scale=None, primary_key=False, nullable=True, unique=False, default=None, references=None, autoincrement=False, backfill=None, enum_name=None, enum_values=None, comment=None)
```
{: .sig}

Use the factories above rather than constructing a `ColumnDef` yourself. Every keyword below works on every factory.

| Option | Type | Meaning |
| --- | --- | --- |
| `primary_key` | `bool` | The column is part of the primary key. Mark several columns for a composite key. |
| `nullable` | `bool` | Whether the column allows NULL. A primary key column is always NOT NULL, whatever you pass. |
| `unique` | `bool` | Adds UNIQUE. The DDL leaves UNIQUE off when the column is also a primary key. |
| `default` | value or `Expression` | A literal default, or raw SQL, as in `Timestamp(default=Expression('CURRENT_TIMESTAMP'))`. |
| `references` | `'table.column'` | Renders `REFERENCES table (column)`. Raises `ValueError` when the string has no dot. |
| `autoincrement` | `bool` | Identity values. Requires an integer type and `primary_key=True`, or the factory raises `ValueError`. |
| `backfill` | value or `Expression` | The value migration generation gives existing rows when it adds this column NOT NULL, or tightens the column to NOT NULL. Plain DDL ignores `backfill`. |
| `length` | `int` | VARCHAR length. |
| `precision`, `scale` | `int` | NUMERIC precision and scale. |
| `enum_name`, `enum_values` | `str`, values | The type name and permitted values of an ENUM column. Valid only there; use the `Enum` factory, which fills both. |
| `comment` | `str` | A description stored in the database catalog on dialects that keep column comments. Must be a non-empty string, or the factory raises `ValueError`. Dialects without column comments render nothing. See [Column comments](/schema#column-comments). |

`default` fills new rows in the database. `backfill` fills the rows that are already in the table, at migration time. A NOT NULL change needs one or the other.

## `Enum`

```python
Enum(*values, name, **options)
```
{: .sig #enum-factory}

`Enum` returns an ENUM `ColumnDef`: a named, ordered list of permitted string values. Pass the values as strings, or pass one Python `enum.Enum` class whose member values are strings. Hydrated values stay plain strings.

`name` is a required keyword. The same name with the same values in two models is one type; the same name with different values raises `ValueError` when the tables render or diff.

`Enum` raises `ValueError` for a missing or empty name, an empty value list, a value that is not a non-empty string, a duplicate value, and a `default` that is not one of the values.

Guide: [Enum columns](/schema#enum-columns).

## `Check`

```python
Check(name, expression)
```
{: .sig}

A named CHECK constraint. List instances in the model's `tableConstraints` attribute. The expression is SQL and renders as written. `Check` raises `ValueError` for an empty name or an empty expression.

## `ForeignKey`

```python
ForeignKey(name, columns, references, on_delete=None, on_update=None)
```
{: .sig}

A named FOREIGN KEY constraint, listed in `tableConstraints`. `columns` is a string or a sequence of the constrained columns, in order. `references` is a `'table.column'` string, or a sequence of them, one per constrained column; every target column must belong to the same table. `on_delete` and `on_update` accept `CASCADE`, `SET NULL`, `RESTRICT`, `NO ACTION`, and `SET DEFAULT`, case-insensitively; `FOREIGN_KEY_ACTIONS` holds that tuple.

`ForeignKey` raises `ValueError` for an empty name, no columns, a column and target count that differ, a target without a dot, targets in more than one table, and an action outside the list.

For a single column with no actions, `references='table.column'` on the column definition renders the same constraint. `TableConstraint` is the union type of `Check` and `ForeignKey`, which is what `tableConstraints` holds.

Guide: [Table constraints](/schema#table-constraints).

### Identity per dialect

| Dialect | `autoincrement` renders |
| --- | --- |
| Default (SQLite) | nothing, because `INTEGER PRIMARY KEY` is a rowid alias |
| Postgres | `GENERATED BY DEFAULT AS IDENTITY` |
| MySQL | `AUTO_INCREMENT` |
| MSSQL | `IDENTITY(1,1)` |
| DuckDB, Presto, Athena | `DialectError` |

The DuckDB message names the alternative: a sequence with a DEFAULT expression.

## `Index`

```python
Index(name, *columns, unique=False)
```
{: .sig #index-factory}

An `Index` holds `name`, `columns`, and `unique`. It raises `ValueError` for an empty name or for no columns.

```python
from sustained.schema import Index

class Show(Model):
    tableName = 'shows'
    tableColumns = {...}
    indexes = [Index('ix_shows_venue', 'venue_id')]
```

`create_table()` creates the indexes, and migration generation keeps them in step with the model. Athena raises `DialectError`, because Athena has no indexes; partition the table instead.

## `TableOptions`

```python
TableOptions(location=None, partitioned_by=None, properties=None)
```
{: .sig}

`TableOptions` holds storage clauses for engines that need them. Athena renders `PARTITIONED BY`, `LOCATION`, and `TBLPROPERTIES` after the column list. Every other dialect raises `DialectError` when the model sets `tableOptions`.

```python
from sustained.schema import TableOptions

class Event(Model):
    tableName = 'events'
    tableColumns = {...}
    tableOptions = TableOptions(
        location='s3://bucket/warehouse/events/',
        partitioned_by=['day(created_at)'],
        properties={'table_type': 'ICEBERG'},
    )
```

Partition entries render as written, so an Iceberg transform reaches the engine unchanged.

## Athena column rules

Athena tables are files in S3 and enforce no constraints, so `validate_column_def` raises `DialectError` for a column that declares `primary_key`, `unique`, `default`, `references`, `autoincrement`, or `nullable=False`. The message lists every problem it found in the column. Declare Athena columns plain and nullable.

## MySQL column rules

MySQL needs a prefix length for a key on a `TEXT`, `JSON`, or `BLOB` column, so `validate_column_def` raises `DialectError` for a `Text()`, `Json()`, or `Binary()` column that declares `unique` or `primary_key`. Use `String(n)` with a length that fits, or declare the prefix index in a hand-written migration. MySQL also takes no literal DEFAULT on those types, so a `default` on them raises as well. Set the value in the application, or give the column a `backfill` so the migration sets it.

## DDL rendering

Both renderers take a compiler, which you get from `Dialects.get_compiler(dialect)`. `Model.create_table_sql()` calls them for you.

```python
render_column_sql(compiler, name, col, inline_pk) -> str
```
{: .sig #render_column_sql}

One column clause, for CREATE TABLE or ADD COLUMN.

```python
build_create_table_sql(compiler, table_sql, columns, if_not_exists=False, options=None, extras=None, constraints=None) -> str
```
{: .sig #build_create_table_sql}

The whole CREATE TABLE statement. `constraints` is a sequence of declared `Check` and `ForeignKey` objects, rendered after the constraints the columns themselves imply.

A single primary key column renders inline. Several primary key columns become a table-level `PRIMARY KEY (...)` constraint.

`build_create_table_sql` raises `ValueError` when `columns` is empty, and when an autoincrement column is combined with a composite primary key.

## `Expression`

```python
Expression(value)
```
{: .sig}

Raw SQL that renders as written, in both `str(query)` and `to_sql()`. Use an `Expression` for a default or a backfill the database computes:

```python
from sustained.schema import Expression, Timestamp

'created_at': Timestamp(default=Expression('CURRENT_TIMESTAMP'))
```

`Expression` is defined in `sustained.types` and re-exported here.
