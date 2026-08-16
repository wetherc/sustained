---
layout: default
title: Predicates and expressions reference
---

Everything in `sustained.expressions`, plus the function registry in
`sustained.functions`. These are the objects that make a query builder
something other than string concatenation: a typed column knows it is a
column, a literal knows it is a literal, and neither can be mistaken for the
other.

Guide: [Filtering](/filtering).

## Typed columns

`col(name)` returns a `ColumnExpr`. So does `Model.c.<column>`, which also
checks the name against the model's declared columns.

```python
from sustained import col

col('venues.capacity') > 1400
Venue.c.capacity > 1400          # the same predicate, with a typo check
```

### `ColumnExpr`

Attribute `name` holds the column path as written.

Comparison operators return a `Predicate`. A `ColumnExpr` on the right side
renders as a column, not as a bound value.

| Operator | Renders | Notes |
| --- | --- | --- |
| `==` | `=` | `== None` renders `IS NULL`. |
| `!=` | `!=` | `!= None` renders `IS NOT NULL`. |
| `>` `>=` `<` `<=` | the same | Comparing to `None` raises `ValueError`. |

Methods, each returning a `Predicate`:

| Signature | Renders |
| --- | --- |
| `like(pattern)` | `LIKE` |
| `not_like(pattern)` | `NOT LIKE` |
| `ilike(pattern)` | `ILIKE`, native on Postgres and DuckDB, `LOWER() LIKE LOWER()` elsewhere |
| `in_(values)` | `IN (...)` over a list or a `QueryBuilder`. An empty list raises `ValueError`. |
| `not_in(values)` | `NOT IN (...)`, same rule |
| `between(low, high)` | `BETWEEN low AND high` |
| `not_between(low, high)` | `NOT BETWEEN low AND high` |
| `is_null()` | `IS NULL` |
| `not_null()` | `IS NOT NULL` |

### `Predicate`

A composable condition. Pass one to `where()` or `having()` as the only
argument.

| Operator | Renders |
| --- | --- |
| `a & b` | `(a AND b)` |
| `a \| b` | `(a OR b)` |
| `~a` | `NOT (a)` |

`bool(predicate)` always raises `TypeError`. That is deliberate: it turns
`a and b` into a loud failure instead of a silent one that evaluates to a
single side. Use `&` and `|`.

## Marking columns and literals

Sustained has to decide whether a bare string is a column name or a value. In
function arguments and CASE results it assumes a column; these two classes
override that.

| Class | Meaning |
| --- | --- |
| `Column(name)` | This string is a column reference or raw SQL. Do not quote it, do not treat it as a value. |
| `Literal(value)` | This value is a literal, even where a column would be assumed. |

```python
from sustained import Literal

query.select_func('COALESCE', 'nickname', 'name', Literal('unknown'), alias='display')
# COALESCE(nickname, name, 'unknown') AS display
```

A string argument that is not a plain column path raises `ValueError` at
render, so a literal cannot silently become a column, or the reverse.

`Expression(value)`, in `sustained.types` and re-exported from
`sustained.schema`, is the same idea for schema defaults: raw SQL that renders
verbatim in both the inline and the parameterized forms.

## Expression objects

The fluent methods on `QueryBuilder` build these. Construct one directly when
you need a shape the fluent method does not cover.

| Class | Signature |
| --- | --- |
| `Func` | `Func(function_name, *args, alias=None)` |
| `AggregateExpression` | `AggregateExpression(function_name, column, alias=None)` |
| `WindowExpression` | `WindowExpression(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)` |
| `CaseExpression` | `CaseExpression(alias, else_result)` |
| `Subquery` | `Subquery(query, alias)` |

`CaseExpression.when(condition, result)` appends a WHEN/THEN pair and returns
itself, so pairs chain. `CaseExpression.whens` returns a copy of the pairs.

`Subquery` embeds a `QueryBuilder` in a SELECT list or a join:

```python
from sustained.expressions import Subquery

ticket_count = Ticket.query().count().where('show_id', '=', Column('shows.id'))

Show.query().select('title', Subquery(ticket_count, 'tickets_sold'))
```

## Function registry

`select_func()` and the fluent function methods check the name against
`FunctionRegistry` in `sustained.functions`. A registered function that the
active dialect cannot spell raises `DialectError` while the query builds. An
unregistered name passes through unchecked, which is how you reach a function
Sustained has never heard of.

| Function | Available on | Per-dialect spelling |
| --- | --- | --- |
| `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` | all six | one spelling |
| `LOWER`, `UPPER`, `COALESCE`, `CONCAT`, `SUBSTRING`, `TRIM`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `MOD` | all six | one spelling |
| `LENGTH` | all six | `LEN` on MSSQL |
| `STRING_AGG` | Postgres, DuckDB, Presto, Athena | one spelling |
| `NOW` | Postgres, DuckDB, Presto, Athena, MSSQL | `GETDATE` on MSSQL |
| `GETDATE` | MSSQL, Postgres, DuckDB, Presto, Athena | `NOW` everywhere but MSSQL |

`NOW()` and `GETDATE()` are the pair worth knowing: write the one you know and
the dialect gets the spelling it needs. Neither is available on the default
dialect, so both raise `DialectError` there.

### Registry API

| Signature | Returns | Description |
| --- | --- | --- |
| `FunctionRegistry.register(name, metadata)` | `None` | Registers or overwrites an entry. The key is the uppercased name. |
| `FunctionRegistry.get_metadata(name)` | `FunctionMetadata` | Case-insensitive lookup. Raises `KeyError` when unregistered. |
| `FunctionRegistry.resolve_name(name, dialect)` | `str` | The dialect's spelling, or the name uppercased. |
| `FunctionRegistry.is_supported(name, dialect)` | `bool` | `True` for anything unregistered. |

`FunctionMetadata(supported_dialects, dialect_names={})` is a `NamedTuple`.
Register your own to get build-time checking for a function Sustained does not
know:

```python
from sustained.dialects import Dialects
from sustained.functions import FunctionMetadata, FunctionRegistry

FunctionRegistry.register(
    'DATE_TRUNC',
    FunctionMetadata(supported_dialects=[Dialects.POSTGRES, Dialects.DUCKDB]),
)
```

## Type aliases

In `sustained.types`, for annotating code that accepts what the builder
accepts.

| Alias | Definition |
| --- | --- |
| `DbReturnValue` | `str \| int \| float \| bool` |
| `Selectable` | Anything `select()` takes |
| `CaseResult` | `DbReturnValue \| Column` |
| `QueryResolvable` | `QueryBuilder \| Callable[..., QueryBuilder] \| str` |
| `Join` | `BasicJoinMapping \| JoinMappingWithThrough` |

The relation-mapping shapes are `TypedDict`s: `RelationMapping`,
`BasicJoinMapping`, `JoinMappingWithThrough`, `ThroughJoinMapping`, and
`ThroughJoinValue`. See [Model](/reference/model#relations).
