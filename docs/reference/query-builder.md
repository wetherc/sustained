---
layout: default
title: QueryBuilder reference
---

`sustained.QueryBuilder` builds every statement Sustained runs. Get one from
`Model.query()`; it is not meant to be constructed directly.

A builder changes in place. Each method adds to the same query and returns the
same object, so a shared base query collects every branch's filters unless you
`clone()` it first.

Guides: [Building Queries](/queries), [Filtering](/filtering),
[Grouping](/grouping), [Relations and Joins](/relations),
[Executing Queries](/executing).

## Construction

| Signature | Returns | Notes |
| --- | --- | --- |
| `QueryBuilder(model_class, dialect=None)` | | `dialect` defaults to `Dialects.DEFAULT`. Use `Model.query()` instead. |
| `str(query)` | `str` | The statement with values inlined as SQL literals. For reading and logging, never for execution. |
| `QueryBuilder.raw(sql)` | `Expression` | Static method. Marks SQL that must render unquoted and unvalidated. |

## Selecting columns

| Signature | Description |
| --- | --- |
| `select(*columns)` | Adds columns to the SELECT list. Accepts strings, the `'col AS alias'` form, `Model.column` references, `ColumnExpr`, and any expression object. Defaults to `*` when never called. |
| `distinct()` | Adds `DISTINCT`. |
| `distinctOn(*columns)` | `DISTINCT ON (...)`. Raises `ValueError` with no columns, or when combined with `distinct()`. Raises `DialectError` at render on every dialect but Postgres and DuckDB. |
| `from_(table, alias=None)` | Overrides the FROM source with a table name or a `QueryBuilder`. Raises `ValueError` when a subquery has no alias, `TypeError` for any other type. |
| `with_(table_alias, subquery, recursive=False)` | Adds a CTE. Raises `TypeError` when `subquery` is not a `QueryBuilder`; at render, `ValueError` when two different subqueries share an alias. MSSQL always renders plain `WITH`. |

### Aggregates

Each returns the builder, so chain them to select several at once.

| Signature | Renders |
| --- | --- |
| `count(column='*', alias=None)` | `COUNT(column)` |
| `sum(column, alias=None)` | `SUM(column)` |
| `avg(column, alias=None)` | `AVG(column)` |
| `min(column, alias=None)` | `MIN(column)` |
| `max(column, alias=None)` | `MAX(column)` |

### Functions, windows, and CASE

| Signature | Description |
| --- | --- |
| `select_func(function_name, *args, alias=None)` | Any SQL function. String arguments are column references; wrap literal values in `Literal()`. A registered function raises `DialectError` on a dialect that has no spelling for it; an unregistered name passes through unchecked. |
| `select_window(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)` | A window function. `order_by` entries may carry a direction (`'price DESC'`). `args` are the function's own arguments. `frame` is a raw frame clause. |
| `select_case(alias, else_result, when_clauses)` | A CASE expression from `(condition, result)` tuples. Results are string literals unless wrapped in `Column()`. |

Every registered function is also a method of its own name, so
`query.coalesce('nick', 'name', alias='display')` and
`query.select_func('COALESCE', ...)` build the same thing. The registered set
and its per-dialect spellings are in
[Predicates and expressions](/reference/predicates#function-registry).

## Filtering

The `where` family is generated, so twelve bases each exist in plain, `and`,
and `or` forms: 36 methods.

| Base | Arguments | Description |
| --- | --- | --- |
| `where` | `(column_or_callable, op=None, val=None)` | A comparison. Also accepts a single `Predicate`, or a callable that receives a builder for a parenthesized group. |
| `whereIn` / `whereNotIn` | `(col, values_or_query)` | IN over a list, a `QueryBuilder`, or a callable. An empty list raises `ValueError`. |
| `whereBetween` / `whereNotBetween` | `(col, low, high)` | Range comparison. |
| `whereExists` / `whereNotExists` | `(query_or_callable)` | EXISTS subquery. |
| `whereLike` / `whereILike` | `(col, pattern)` | LIKE, and case-insensitive LIKE. |
| `whereNull` / `whereNotNull` | `(col)` | `IS NULL` / `IS NOT NULL`. |
| `whereRaw` | `(sql, params=None)` | A raw fragment with `?` value markers. Values still parameterize. Renders wrapped in parentheses. |

Prefix each with `and` or `or` for the conjunction: `andWhereIn`,
`orWhereNotBetween`, and so on. The first condition in a chain must be a plain
`where`; an `and`/`or` form there raises `RuntimeError`.

Errors from the `where` family:

| Condition | Raises |
| --- | --- |
| A `Predicate` passed together with `op` or `val` | `ValueError` |
| `op` is `None` on a non-callable clause | `ValueError` |
| `val` is `None` with an operator other than `=`, `!=`, `<>`, `IS`, `IS NOT` | `ValueError` |
| An operator outside the allowlist | `ValueError` |
| `whereRaw` marker count does not match the parameter count | `ValueError` |

The operator allowlist is `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`,
`NOT LIKE`, `ILIKE`, `NOT ILIKE`, `IS`, `IS NOT`. Comparing to `None` with
`=` or `!=` renders `IS NULL` or `IS NOT NULL`. `ILIKE` is native on Postgres
and DuckDB and compiles to `LOWER(col) LIKE LOWER(pattern)` elsewhere, so it
never raises.

## Grouping and window filters

| Signature | Description |
| --- | --- |
| `groupBy(*columns)` | GROUP BY. |
| `groupByRollup(*columns)` | `GROUP BY ROLLUP (...)`. Raises `ValueError` with no columns. |
| `groupByCube(*columns)` | `GROUP BY CUBE (...)`. Raises `ValueError` with no columns. |
| `groupByGroupingSets(*tuples)` | `GROUP BY GROUPING SETS (...)`. An empty tuple is the grand total. Raises `ValueError` with no sets. |
| `qualify(condition)` | Filters on window results. Takes a `Predicate` or a raw string. Raises `DialectError` at render on every dialect but DuckDB. |

The `having` family mirrors the `where` family exactly: the same twelve bases,
the same three prefixes, the same arguments and errors. `having`,
`andHavingIn`, `orHavingNotBetween`, `havingRaw`, and the rest all exist.
Filter on the aggregate as written (`having('COUNT(id)', '>', 10)`), because
standard SQL does not expose SELECT aliases to HAVING.

## Ordering, paging, and locking

| Signature | Description |
| --- | --- |
| `orderBy(column, direction='asc')` | Sorts. Chain calls for several columns. Any direction other than `asc` or `desc` raises `ValueError`. |
| `limit(value)` | Caps rows. Raises `ValueError` if set twice or combined with `top()`, `TypeError` for a non-integer. |
| `offset(value)` | Skips rows. Raises `ValueError` if set twice. |
| `top(value)` | `TOP n`. MSSQL only; every other dialect raises `DialectError` at render. Mutually exclusive with `limit()`. |
| `page(page, page_size)` | LIMIT and OFFSET from a zero-based page number. |
| `cursor_page(column, page_size, after=None)` | Keyset pagination: orders by the column, filters past `after`, and limits. The cost does not grow with depth. |
| `for_update(skip_locked=False, nowait=False)` | Row locking. Postgres only. Raises `ValueError` when both flags are set, or when combined with a union. |
| `clone()` | A deep copy of the builder. The only method that does not return `self`. |

On MSSQL, `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL
allows only after an `ORDER BY`; without one they raise `DialectError` at
render. On Presto and Athena, `OFFSET` renders before `LIMIT`.

## Joining

Nine join types, each in a raw form and a relation form: 18 methods.

Types: `join`, `innerJoin`, `leftJoin`, `leftOuterJoin`, `rightJoin`,
`rightOuterJoin`, `fullJoin`, `fullOuterJoin`, `crossJoin`.

**Raw form.** `join(table, ...)` takes one of three shapes:

```python
query.join('venues', 'shows.venue_id', '=', 'venues.id')   # ON condition
query.join('profiles', using=['profile_id'])               # USING list
query.join('venues', lambda j: j.on(...).orOn(...))        # lambda
```

The table argument must be a table name. To join a derived result set, put it
in a CTE with `with_()` and join the CTE by its alias. Mixing `using` with
positional arguments raises `ValueError`; a non-list `using` raises
`TypeError`.

**Relation form.** `joinRelated(relation_name, alias=None)` and the same
eight prefixed variants. The join condition comes from `relationMappings`. A
`ManyToManyRelation` joins the through table first, always with an INNER JOIN;
the join type you name applies to the second hop. An unknown relation name, or
a mapping missing `modelClass`, `join`, `from`, or `to`, raises `ValueError`.

### `OnClauseBuilder`

The object a join lambda receives.

| Signature | Description |
| --- | --- |
| `on(col1, op, col2)` | The first ON condition. `col2` may be a `QueryBuilder`, which renders as a parenthesized subquery. |
| `andOn(col1, op, col2)` | An AND condition. Raises `RuntimeError` as the first call. |
| `orOn(col1, op, col2)` | An OR condition. Raises `RuntimeError` as the first call. |

A lambda that adds no condition raises `RuntimeError` at render.

## Combining queries

| Signature | Description |
| --- | --- |
| `union(*queries, all=False)` | UNION, or UNION ALL when `all=True`. |
| `unionAll(*queries)` | UNION ALL. |
| `intersect(*queries)` | INTERSECT. |
| `except_(*queries)` | EXCEPT. The trailing underscore avoids the Python keyword. |

`ORDER BY`, `LIMIT`, and `OFFSET` on the outer query apply to the whole
result. The same clauses on a member render inside that member's parentheses.
CTEs from every member hoist to one top-level `WITH`.

## Writing data

| Signature | Description |
| --- | --- |
| `insert(values)` | INSERT from a dict or a list of dicts. Raises `ValueError` on an empty list, a row with no columns, or rows whose columns differ. |
| `insert_from(columns, query)` | `INSERT ... SELECT`. `columns=None` inserts positionally. Raises `TypeError` when `query` is not a `QueryBuilder`. |
| `update(values)` | UPDATE. Raises `ValueError` on an empty dict, and at render when the query has no `where()`. |
| `delete()` | DELETE. Raises `ValueError` at render when the query has no `where()`. |
| `onConflict(*columns)` | Declares the upsert conflict target. Raises `ValueError` unless it follows `insert()`, with no columns, or when a conflict column was not inserted. |
| `merge(columns=None)` | On conflict, update. Defaults to every inserted non-conflict column. Raises `ValueError` without `onConflict()`, and at render when there is nothing left to update. |
| `ignore()` | On conflict, skip the row. Raises `ValueError` without `onConflict()`. |
| `returning(*columns)` | RETURNING. Defaults to `*`. The statement then returns dicts instead of a row count. |
| `create_table_as(table_name, temporary=False)` | CREATE TABLE AS from a SELECT. Raises `ValueError` on a non-SELECT or an empty name. |

An `UPDATE` or `DELETE` without a filter raises rather than touching every
row. To mean it, add `where(QueryBuilder.raw('1'), '=', 1)`.

Dialect refusals: upserts raise on Presto; `RETURNING` raises on MSSQL,
Presto, and Athena; `create_table_as` raises on MSSQL, and on Athena when
`temporary=True`.

## Rendering and execution

| Signature | Returns | Description |
| --- | --- | --- |
| `to_sql()` | `(sql, params)` | The parameterized statement and its values in SQL order. |
| `run(connection=None)` | models, `int`, or dicts | Executes. SELECT returns hydrated instances with eager relations attached. Writes commit, unless inside a `transaction()` block, and return the affected row count, or dicts when `RETURNING` is set. |
| `first(connection=None)` | `Model` or `None` | Runs a clone with `LIMIT 1`. Leaves the original builder alone. |
| `to_dicts(connection=None)` | `list[dict]` | Rows keyed by column name. No hydration, no eager loading. |
| `to_df(connection=None)` | `pandas.DataFrame` | Raises `RuntimeError` when pandas is missing. |
| `to_arrow(connection=None)` | `pyarrow.Table` | Raises `RuntimeError` when pyarrow is missing. |
| `total(connection=None)` | `int` | `SELECT COUNT(*)` over the query with ordering, LIMIT, OFFSET, and TOP stripped. The builder is unchanged. |
| `explain(connection=None, analyze=False)` | `list[tuple]` | The query plan. `analyze=True` executes the statement. Raises `DialectError` on MSSQL. |
| `withGraphFetched(*relation_names)` | builder | Eager loads relations, one query per relation per level. A name may be a dotted path such as `'shows.tickets'`. Raises `ValueError` for an unknown relation name or path segment. |

`to_dicts`, `to_df`, `to_arrow`, and `total` raise `ValueError` on anything
that is not a SELECT.

A multi-row `insert()` with no RETURNING runs through the driver's
`executemany()`.

### Async

| Signature | Returns |
| --- | --- |
| `await arun(adapter=None)` | The same shapes as `run()` |
| `await afirst(adapter=None)` | `Model` or `None` |
| `await ato_dicts(adapter=None)` | `list[dict]` |

The adapter resolves in this order: the argument, an open
`async_transaction()`, then `Model.bind_async()`. With none of the three,
`RuntimeError`.

## Connection resolution

Every execution method resolves a connection the same way: the `connection`
argument first, then a connection pinned by an open `transaction()` block,
then the binding from `Model.bind()`. With none of the three, `RuntimeError`.
