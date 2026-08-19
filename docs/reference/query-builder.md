---
layout: default
title: QueryBuilder reference
---

`sustained.QueryBuilder` builds every statement Sustained runs. Get a builder from `Model.query()` rather than constructing one yourself.

A builder changes in place. Each method adds to the same query and returns the same builder, so a shared base query collects the filters of every branch unless you `clone()` it first.

Guides: [Building Queries](/queries), [Filtering](/filtering), [Grouping](/grouping), [Relations and Joins](/relations), [Executing Queries](/executing).

## Construction

```python
QueryBuilder(model_class, dialect=None)
```
{: .sig #querybuilder}

`dialect` defaults to `Dialects.DEFAULT`. Use `Model.query()` instead.

```python
str(query) -> str
```
{: .sig #str}

The statement with values inlined as SQL literals. Use it to read and log a query. To execute a query, use `run()` or `to_sql()`.

```python
QueryBuilder.raw(sql) -> Expression
```
{: .sig #raw}

Static method. Marks SQL that renders unquoted and unvalidated.

## Selecting columns

```python
select(*columns)
```
{: .sig #select}

Adds columns to the SELECT list. Accepts strings, the `'col AS alias'` form, `Model.column` references, `ColumnExpr`, and any expression object. The SELECT list defaults to `*` when you never call `select()`.

```python
distinct()
```
{: .sig #distinct}

Adds `DISTINCT`.

```python
distinctOn(*columns)
```
{: .sig #distincton}

`DISTINCT ON (...)`. Raises `ValueError` with no columns, or when combined with `distinct()`. Raises `DialectError` at render time on every dialect but Postgres and DuckDB.

```python
from_(table, alias=None)
```
{: .sig #from_}

Overrides the FROM source with a table name or a `QueryBuilder`. Raises `ValueError` when a subquery has no alias, and `TypeError` for any other type.

```python
with_(table_alias, subquery, recursive=False)
```
{: .sig #with_}

Adds a CTE. Raises `TypeError` when `subquery` is not a `QueryBuilder`, and `ValueError` at render time when two different subqueries share an alias. MSSQL always renders plain `WITH`.

### Aggregates

Each aggregate takes `(column, alias=None)` and returns the builder, so chain them to select several at once. `count()` alone defaults its column to `'*'`.

| Method | Renders |
| --- | --- |
| `count` | `COUNT(column)` |
| `sum` | `SUM(column)` |
| `avg` | `AVG(column)` |
| `min` | `MIN(column)` |
| `max` | `MAX(column)` |

### Functions, windows, and CASE

```python
select_func(function_name, *args, alias=None)
```
{: .sig #select_func}

Any SQL function. String arguments are column references; wrap literal values in `Literal()`. A registered function raises `DialectError` on a dialect that has no spelling for it. An unregistered name passes through unchecked.

```python
select_window(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)
```
{: .sig #select_window}

A window function. An `order_by` entry may carry a direction, as in `'price DESC'`. `args` are the function's own arguments. `frame` is a raw frame clause.

```python
select_case(alias, else_result, when_clauses)
```
{: .sig #select_case}

A CASE expression from `(condition, result)` tuples. Results are string literals unless you wrap them in `Column()`.

Every registered function is also a method of its own name, so `query.coalesce('nick', 'name', alias='display')` and `query.select_func('COALESCE', ...)` build the same expression. The registered functions and their per-dialect spellings are in [Predicates and expressions](/reference/predicates#function-registry).

## Filtering

The `where` family is generated. Every base below exists in a plain, an `and`, and an `or` form.

```python
where(column_or_callable, op=None, val=None)
```
{: .sig #where}

A comparison. Also accepts a single `Predicate`, or a callable that receives a builder for a parenthesized group.

```python
whereIn(col, values_or_query)
```
{: .sig #wherein}

IN over a list, a `QueryBuilder`, or a callable. An empty list raises `ValueError`. `whereNotIn` builds the NOT IN form.

```python
whereBetween(col, low, high)
```
{: .sig #wherebetween}

Range comparison. `whereNotBetween` builds the NOT BETWEEN form.

```python
whereExists(query_or_callable)
```
{: .sig #whereexists}

EXISTS subquery. `whereNotExists` builds the NOT EXISTS form.

```python
whereLike(col, pattern)
```
{: .sig #wherelike}

LIKE. `whereILike` is the case-insensitive form.

```python
whereNull(col)
```
{: .sig #wherenull}

`IS NULL`. `whereNotNull` renders `IS NOT NULL`.

```python
whereRaw(sql, params=None)
```
{: .sig #whereraw}

A raw fragment with `?` value markers. The values still parameterize. The fragment renders wrapped in parentheses.

Prefix a base with `and` or `or` for the conjunction, as in `andWhereIn` or `orWhereNotBetween`. The first condition in a chain must be a plain `where`; an `and` or `or` form in that position raises `RuntimeError`.

Errors from the `where` family:

| Condition | Raises |
| --- | --- |
| A `Predicate` passed together with `op` or `val` | `ValueError` |
| `op` is `None` on a clause that is not a callable | `ValueError` |
| `val` is `None` with an operator other than `=`, `!=`, `<>`, `IS`, `IS NOT` | `ValueError` |
| An operator outside the allowlist | `ValueError` |
| A `whereRaw` marker count that does not match the parameter count | `ValueError` |

The operator allowlist is `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`, `ILIKE`, `NOT ILIKE`, `IS`, and `IS NOT`. Comparing to `None` with `=` or `!=` renders `IS NULL` or `IS NOT NULL`. `ILIKE` is native on Postgres and DuckDB and compiles to `LOWER(col) LIKE LOWER(pattern)` everywhere else, so `ILIKE` never raises.

## Grouping and window filters

```python
groupBy(*columns)
```
{: .sig #groupby}

GROUP BY.

```python
groupByRollup(*columns)
```
{: .sig #groupbyrollup}

`GROUP BY ROLLUP (...)`. Raises `ValueError` with no columns.

```python
groupByCube(*columns)
```
{: .sig #groupbycube}

`GROUP BY CUBE (...)`. Raises `ValueError` with no columns.

```python
groupByGroupingSets(*tuples)
```
{: .sig #groupbygroupingsets}

`GROUP BY GROUPING SETS (...)`. An empty tuple is the grand total. Raises `ValueError` with no sets.

```python
qualify(condition)
```
{: .sig #qualify}

Filters on window results. Takes a `Predicate` or a raw string. Raises `DialectError` at render time on every dialect but DuckDB.

The `having` family mirrors the `where` family: the same bases, the same three prefixes, the same arguments, and the same errors. `having`, `andHavingIn`, `orHavingNotBetween`, and `havingRaw` all exist. Filter on the aggregate as written, as in `having('COUNT(id)', '>', 10)`, because standard SQL does not expose SELECT aliases to HAVING.

## Ordering, paging, and locking

```python
orderBy(column, direction='asc')
```
{: .sig #orderby}

Sorts the result. Chain calls to sort by several columns. Any direction other than `asc` or `desc` raises `ValueError`.

```python
limit(value)
```
{: .sig #limit}

Caps the row count. Raises `ValueError` when set twice or combined with `top()`, and `TypeError` for a value that is not an integer.

```python
offset(value)
```
{: .sig #offset}

Skips rows. Raises `ValueError` when set twice.

```python
top(value)
```
{: .sig #top}

`TOP n`. MSSQL only; every other dialect raises `DialectError` at render time. `top()` and `limit()` are mutually exclusive.

```python
page(page, page_size)
```
{: .sig #page}

LIMIT and OFFSET from a zero-based page number.

```python
cursor_page(column, page_size, after=None)
```
{: .sig #cursor_page}

Keyset pagination: orders by the column, filters past the `after` value, and limits the row count. The database does not scan the skipped rows, so a deep page costs the same as the first one.

```python
for_update(skip_locked=False, nowait=False)
```
{: .sig #for_update}

Row locking. Postgres only. Raises `ValueError` when both flags are set, or when combined with a union.

```python
clone() -> QueryBuilder
```
{: .sig #clone}

A deep copy of the builder. Every other method returns the same builder.

On MSSQL, `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL allows only after an `ORDER BY`. Without an `ORDER BY` they raise `DialectError` at render time. On Presto and Athena, `OFFSET` renders before `LIMIT`.

## Joining

Every join type exists in a raw form and a relation form. The types are `join`, `innerJoin`, `leftJoin`, `leftOuterJoin`, `rightJoin`, `rightOuterJoin`, `fullJoin`, `fullOuterJoin`, and `crossJoin`.

**Raw form.** `join(table, ...)` takes one of three shapes:

```python
query.join('venues', 'shows.venue_id', '=', 'venues.id')   # ON condition
query.join('profiles', using=['profile_id'])               # USING list
query.join('venues', lambda j: j.on(...).orOn(...))        # lambda
```

The table argument must be a table name. To join a derived result set, put it in a CTE with `with_()` and join the CTE by its alias. Mixing `using` with positional arguments raises `ValueError`, and a `using` value that is not a list raises `TypeError`.

**Relation form.** `joinRelated(relation_name, alias=None)`, and the same prefixed variant for every other join type. The join condition comes from `relationMappings`. A `ManyToManyRelation` joins the through table first, always with an INNER JOIN; the join type you name applies to the second hop. An unknown relation name raises `ValueError`, and so does a mapping that is missing `modelClass`, `join`, `from`, or `to`.

### `OnClauseBuilder`

`OnClauseBuilder` is the object a join lambda receives.

```python
on(col1, op, col2)
```
{: .sig #on}

The first ON condition. `col2` may be a `QueryBuilder`, which renders as a parenthesized subquery. `andOn` and `orOn` add further conditions with the same arguments; either one raises `RuntimeError` as the first call.

A lambda that adds no condition raises `RuntimeError` at render time.

## Combining queries

```python
union(*queries, all=False)
```
{: .sig #union}

UNION, or UNION ALL when `all=True`. `unionAll(*queries)` is the UNION ALL shorthand.

```python
intersect(*queries)
```
{: .sig #intersect}

INTERSECT.

```python
except_(*queries)
```
{: .sig #except_}

EXCEPT. The trailing underscore keeps the name off the Python keyword.

`ORDER BY`, `LIMIT`, and `OFFSET` on the outer query apply to the whole result. The same clauses on a member query render inside that member's parentheses. CTEs from every member hoist to one top-level `WITH`.

## Writing data

```python
insert(values)
```
{: .sig #insert}

INSERT from a dict or a list of dicts. Raises `ValueError` on an empty list, on a row with no columns, and on rows whose columns differ from each other.

```python
insert_from(columns, query)
```
{: .sig #insert_from}

`INSERT ... SELECT`. `columns=None` inserts positionally. Raises `TypeError` when `query` is not a `QueryBuilder`.

```python
update(values)
```
{: .sig #update}

UPDATE. Raises `ValueError` on an empty dict, and at render time when the query has no `where()`.

```python
delete()
```
{: .sig #delete}

DELETE. Raises `ValueError` at render time when the query has no `where()`.

```python
onConflict(*columns)
```
{: .sig #onconflict}

Declares the upsert conflict target. Raises `ValueError` when it does not follow `insert()`, when it gets no columns, and when a conflict column was not one of the inserted columns.

```python
merge(columns=None)
```
{: .sig #merge}

Updates the row on conflict. Defaults to every inserted column that is not a conflict column. Raises `ValueError` without `onConflict()`, and at render time when no column is left to update.

```python
ignore()
```
{: .sig #ignore}

Skips the row on conflict. Raises `ValueError` without `onConflict()`.

```python
returning(*columns)
```
{: .sig #returning}

RETURNING. Defaults to `*`. The statement then returns dicts instead of a row count.

```python
create_table_as(table_name, temporary=False)
```
{: .sig #create_table_as}

CREATE TABLE AS from a SELECT. Raises `ValueError` on a statement that is not a SELECT, and on an empty table name.

An `UPDATE` or `DELETE` with no filter raises instead of touching every row. To update or delete every row on purpose, add `where(QueryBuilder.raw('1'), '=', 1)`.

Dialect refusals: upserts raise on Presto; `RETURNING` raises on MSSQL, Presto, and Athena; `create_table_as` raises on MSSQL, and on Athena when `temporary=True`.

## Rendering and execution

```python
to_sql() -> (sql, params)
```
{: .sig #to_sql}

The parameterized statement, and its values in SQL order.

```python
run(connection=None)
```
{: .sig #run}

Executes the statement. A SELECT returns hydrated instances with eager relations attached. A write commits, unless it runs inside a `transaction()` block, and returns the affected row count, or dicts when `RETURNING` is set.

```python
first(connection=None) -> Model | None
```
{: .sig #first}

Runs a clone of the builder with `LIMIT 1`. The original builder is unchanged.

```python
to_dicts(connection=None) -> list[dict]
```
{: .sig #to_dicts}

Rows keyed by column name, with no hydration and no eager loading.

```python
to_df(connection=None) -> pandas.DataFrame
```
{: .sig #to_df}

Raises `RuntimeError` when pandas is not installed.

```python
to_arrow(connection=None) -> pyarrow.Table
```
{: .sig #to_arrow}

Raises `RuntimeError` when pyarrow is not installed.

```python
total(connection=None) -> int
```
{: .sig #total}

`SELECT COUNT(*)` over the query with the ordering, LIMIT, OFFSET, and TOP stripped. The builder is unchanged.

```python
explain(connection=None, analyze=False) -> list[tuple]
```
{: .sig #explain}

The query plan. `analyze=True` executes the statement. Raises `DialectError` on MSSQL.

```python
withGraphFetched(*relation_names)
```
{: .sig #withgraphfetched}

Eager loads relations, with one query per relation per level. A name may be a dotted path such as `'shows.tickets'`. Raises `ValueError` for an unknown relation name or path segment.

`to_dicts`, `to_df`, `to_arrow`, and `total` raise `ValueError` on any statement that is not a SELECT.

A multi-row `insert()` with no RETURNING runs through the driver's `executemany()`.

### Async

```python
await arun(adapter=None)
```
{: .sig #arun}

Returns the same shapes as `run()`.

```python
await afirst(adapter=None) -> Model | None
```
{: .sig #afirst}

The async form of `first()`.

```python
await ato_dicts(adapter=None) -> list[dict]
```
{: .sig #ato_dicts}

The async form of `to_dicts()`.

The adapter resolves in this order: the `adapter` argument, then an open `async_transaction()`, then `Model.bind_async()`. When none of the three resolves, the call raises `RuntimeError`.

## Connection resolution

Every execution method resolves a connection the same way: the `connection` argument first, then a connection pinned by an open `transaction()` block, then the binding from `Model.bind()`. When none of the three resolves, the call raises `RuntimeError`.
