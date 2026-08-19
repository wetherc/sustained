---
layout: default
title: Predicates and expressions reference
---

Everything in `sustained.expressions`, plus the function registry in `sustained.functions`. These objects keep columns, literals, and conditions apart from one another, so the builder never has to guess which one a string was meant to be.

Guide: [Filtering](/filtering).

## Typed columns

`col(name)` returns a `ColumnExpr`. `Model.c.<column>` returns one too, and also checks the name against the model's declared columns.

```python
from sustained import col

col('venues.capacity') > 1400
Venue.c.capacity > 1400          # the same predicate, with a typo check
```

### `ColumnExpr`

The `name` attribute holds the column path as you wrote it.

Comparison operators return a `Predicate`. A `ColumnExpr` on the right side of a comparison renders as a column, not as a bound value.

| Operator | Renders | Notes |
| --- | --- | --- |
| `==` | `=` | `== None` renders `IS NULL`. |
| `!=` | `!=` | `!= None` renders `IS NOT NULL`. |
| `>` `>=` `<` `<=` | the same operator | Comparing to `None` raises `ValueError`. |

Each method below returns a `Predicate`.

| Method | Renders |
| --- | --- |
| `like(pattern)` | `LIKE` |
| `not_like(pattern)` | `NOT LIKE` |
| `ilike(pattern)` | `ILIKE`, native on Postgres and DuckDB, `LOWER() LIKE LOWER()` elsewhere |
| `in_(values)` | `IN (...)` over a list or a `QueryBuilder`. An empty list raises `ValueError`. |
| `not_in(values)` | `NOT IN (...)`. An empty list raises `ValueError`. |
| `between(low, high)` | `BETWEEN low AND high` |
| `not_between(low, high)` | `NOT BETWEEN low AND high` |
| `is_null()` | `IS NULL` |
| `not_null()` | `IS NOT NULL` |

### `Predicate`

A composable condition. Pass a `Predicate` to `where()` or `having()` as the only argument.

| Operator | Renders |
| --- | --- |
| `a & b` | `(a AND b)` |
| `a \| b` | `(a OR b)` |
| `~a` | `NOT (a)` |

`bool(predicate)` always raises `TypeError`, so `a and b` fails instead of evaluating to one side of the expression. Use `&` and `|`.

## Marking columns and literals

Sustained decides whether a bare string is a column name or a value. In function arguments and CASE results it reads the string as a column. These two classes override that reading.

```python
Column(name)
```
{: .sig #column}

The string is a column reference or raw SQL. Sustained does not quote it and does not treat it as a value.

```python
Literal(value)
```
{: .sig #literal}

The value is a literal, even in a position where Sustained would read a column.

```python
from sustained import Literal

query.select_func('COALESCE', 'nickname', 'name', Literal('unknown'), alias='display')

# COALESCE(nickname, name, 'unknown') AS display
```

A string argument that is not a plain column path raises `ValueError` at render time.

`Expression(value)`, in `sustained.types` and re-exported from `sustained.schema`, does the same job for schema defaults: raw SQL that renders as written in both the inline and the parameterized forms.

## Expression objects

The fluent methods on `QueryBuilder` build these objects for you. Construct one directly when you need a shape the fluent method does not cover.

```python
Func(function_name, *args, alias=None)
```
{: .sig #func}

A function call, the object `select_func()` builds.

```python
AggregateExpression(function_name, column, alias=None)
```
{: .sig #aggregateexpression}

An aggregate, the object `count()` and its siblings build.

```python
WindowExpression(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)
```
{: .sig #windowexpression}

A window function, the object `select_window()` builds.

```python
CaseExpression(alias, else_result)
```
{: .sig #caseexpression}

A CASE expression. `when(condition, result)` appends a WHEN/THEN pair and returns the `CaseExpression`, so pairs chain. `whens` returns a copy of the pairs.

```python
Subquery(query, alias)
```
{: .sig #subquery}

Embeds a `QueryBuilder` in a SELECT list or a join:

```python
from sustained.expressions import Subquery

ticket_count = (Ticket.query()
    .count()
    .where('show_id', '=', Column('shows.id'))
)

Show.query().select('title', Subquery(ticket_count, 'tickets_sold'))
```

## Function registry

`select_func()` and the fluent function methods check the name against `FunctionRegistry` in `sustained.functions`. A registered function that the active dialect cannot spell raises `DialectError` while the query builds. An unregistered name passes through unchecked, so you can call a function the registry does not list.

| Function | Available on | Per-dialect spelling |
| --- | --- | --- |
| `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` | every dialect | one spelling |
| `LOWER`, `UPPER`, `COALESCE`, `CONCAT`, `SUBSTRING`, `TRIM`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `MOD` | every dialect | one spelling |
| `LENGTH` | every dialect | `LEN` on MSSQL |
| `STRING_AGG` | Postgres, DuckDB, Presto, Athena | one spelling |
| `NOW` | Postgres, MySQL, DuckDB, Presto, Athena | `GETDATE` on MSSQL |
| `GETDATE` | MSSQL | `NOW` on Postgres, MySQL, DuckDB, Presto, and Athena |

Write either `NOW()` or `GETDATE()` and the dialect renders its own spelling. Neither one is registered for the default dialect, so both raise `DialectError` there.

`STRING_AGG` is left off MySQL on purpose. MySQL spells the same idea as `GROUP_CONCAT`, which takes its separator as a `SEPARATOR` keyword rather than a second argument, so a renamed function would produce SQL that does not parse. Write `GROUP_CONCAT` through raw SQL there.

### Registry API

```python
FunctionRegistry.register(name, metadata)
```
{: .sig #register}

Registers or overwrites an entry. The key is the uppercased name.

```python
FunctionRegistry.get_metadata(name) -> FunctionMetadata
```
{: .sig #get_metadata}

Case-insensitive lookup. Raises `KeyError` when the name is unregistered.

```python
FunctionRegistry.resolve_name(name, dialect) -> str
```
{: .sig #resolve_name}

The dialect's spelling, or the name uppercased.

```python
FunctionRegistry.is_supported(name, dialect) -> bool
```
{: .sig #is_supported}

`True` for any unregistered name.

`FunctionMetadata(supported_dialects, dialect_names={})` is a `NamedTuple`. Register your own metadata to get build-time checking for a function the registry does not list:

```python
from sustained.dialects import Dialects
from sustained.functions import FunctionMetadata, FunctionRegistry

FunctionRegistry.register(
    'DATE_TRUNC',
    FunctionMetadata(supported_dialects=[Dialects.POSTGRES, Dialects.DUCKDB]),
)
```

## Type aliases

These live in `sustained.types`. Use them to annotate code that accepts what the builder accepts.

| Alias | Definition |
| --- | --- |
| `DbReturnValue` | `str \| int \| float \| bool \| datetime \| date \| Decimal \| bytes` |
| `Selectable` | Anything `select()` takes |
| `CaseResult` | `DbReturnValue \| Column` |
| `QueryResolvable` | `QueryBuilder \| Callable[..., QueryBuilder] \| str` |
| `Join` | `BasicJoinMapping \| JoinMappingWithThrough` |

The relation-mapping shapes are `TypedDict`s: `RelationMapping`, `BasicJoinMapping`, `JoinMappingWithThrough`, `ThroughJoinMapping`, and `ThroughJoinValue`. See [Model](/reference/model#relations).
