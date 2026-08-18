---
layout: default
title: QueryBuilder reference
---

`sustained.QueryBuilder` builds every statement Sustained runs. Get a builder from `Model.query()` rather than constructing one yourself.

A builder changes in place. Each method adds to the same query and returns the same builder, so a shared base query collects the filters of every branch unless you `clone()` it first.

Guides: [Building Queries](/queries), [Filtering](/filtering), [Grouping](/grouping), [Relations and Joins](/relations), [Executing Queries](/executing).

## Construction

| Signature | Returns | Notes |
| --- | --- | --- |
| `QueryBuilder(model_class, dialect=None)` | | `dialect` defaults to `Dialects.DEFAULT`. Use `Model.query()` instead. |
| `str(query)` | `str` | The statement with values inlined as SQL literals. Use it to read and log a query. To execute a query, use `run()` or `to_sql()`. |
| `QueryBuilder.raw(sql)` | `Expression` | Static method. Marks SQL that renders unquoted and unvalidated. |

## Selecting columns

| Signature | Description |
| --- | --- |
| `select(*columns)` | Adds columns to the SELECT list. Accepts strings, the `'col AS alias'` form, `Model.column` references, `ColumnExpr`, and any expression object. The SELECT list defaults to `*` when you never call `select()`. |
| `distinct()` | Adds `DISTINCT`. |
| `distinctOn(*columns)` | `DISTINCT ON (...)`. Raises `ValueError` with no columns, or when combined with `distinct()`. Raises `DialectError` at render time on every dialect but Postgres and DuckDB. |
| `from_(table, alias=None)` | Overrides the FROM source with a table name or a `QueryBuilder`. Raises `ValueError` when a subquery has no alias, and `TypeError` for any other type. |
| `with_(table_alias, subquery, recursive=False)` | Adds a CTE. Raises `TypeError` when `subquery` is not a `QueryBuilder`, and `ValueError` at render time when two different subqueries share an alias. MSSQL always renders plain `WITH`. |

### Aggregates

Each aggregate returns the builder, so chain them to select several at once.

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
| `select_func(function_name, *args, alias=None)` | Any SQL function. String arguments are column references; wrap literal values in `Literal()`. A registered function raises `DialectError` on a dialect that has no spelling for it. An unregistered name passes through unchecked. |
| `select_window(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)` | A window function. An `order_by` entry may carry a direction, as in `'price DESC'`. `args` are the function's own arguments. `frame` is a raw frame clause. |
| `select_case(alias, else_result, when_clauses)` | A CASE expression from `(condition, result)` tuples. Results are string literals unless you wrap them in `Column()`. |

Every registered function is also a method of its own name, so `query.coalesce('nick', 'name', alias='display')` and `query.select_func('COALESCE', ...)` build the same expression. The registered functions and their per-dialect spellings are in [Predicates and expressions](/reference/predicates#function-registry).

## Filtering

The `where` family is generated. Every base below exists in a plain, an `and`, and an `or` form.

| Base | Arguments | Description |
| --- | --- | --- |
| `where` | `(column_or_callable, op=None, val=None)` | A comparison. Also accepts a single `Predicate`, or a callable that receives a builder for a parenthesized group. |
| `whereIn` / `whereNotIn` | `(col, values_or_query)` | IN over a list, a `QueryBuilder`, or a callable. An empty list raises `ValueError`. |
| `whereBetween` / `whereNotBetween` | `(col, low, high)` | Range comparison. |
| `whereExists` / `whereNotExists` | `(query_or_callable)` | EXISTS subquery. |
| `whereLike` / `whereILike` | `(col, pattern)` | LIKE, and case-insensitive LIKE. |
| `whereNull` / `whereNotNull` | `(col)` | `IS NULL` and `IS NOT NULL`. |
| `whereRaw` | `(sql, params=None)` | A raw fragment with `?` value markers. The values still parameterize. The fragment renders wrapped in parentheses. |

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

| Signature | Description |
| --- | --- |
| `groupBy(*columns)` | GROUP BY. |
| `groupByRollup(*columns)` | `GROUP BY ROLLUP (...)`. Raises `ValueError` with no columns. |
| `groupByCube(*columns)` | `GROUP BY CUBE (...)`. Raises `ValueError` with no columns. |
| `groupByGroupingSets(*tuples)` | `GROUP BY GROUPING SETS (...)`. An empty tuple is the grand total. Raises `ValueError` with no sets. |
| `qualify(condition)` | Filters on window results. Takes a `Predicate` or a raw string. Raises `DialectError` at render time on every dialect but DuckDB. |

The `having` family mirrors the `where` family: the same bases, the same three prefixes, the same arguments, and the same errors. `having`, `andHavingIn`, `orHavingNotBetween`, and `havingRaw` all exist. Filter on the aggregate as written, as in `having('COUNT(id)', '>', 10)`, because standard SQL does not expose SELECT aliases to HAVING.

## Ordering, paging, and locking

| Signature | Description |
| --- | --- |
| `orderBy(column, direction='asc')` | Sorts the result. Chain calls to sort by several columns. Any direction other than `asc` or `desc` raises `ValueError`. |
| `limit(value)` | Caps the row count. Raises `ValueError` when set twice or combined with `top()`, and `TypeError` for a value that is not an integer. |
| `offset(value)` | Skips rows. Raises `ValueError` when set twice. |
| `top(value)` | `TOP n`. MSSQL only; every other dialect raises `DialectError` at render time. `top()` and `limit()` are mutually exclusive. |
| `page(page, page_size)` | LIMIT and OFFSET from a zero-based page number. |
| `cursor_page(column, page_size, after=None)` | Keyset pagination: orders by the column, filters past the `after` value, and limits the row count. The database does not scan the skipped rows, so a deep page costs the same as the first one. |
| `for_update(skip_locked=False, nowait=False)` | Row locking. Postgres only. Raises `ValueError` when both flags are set, or when combined with a union. |
| `clone()` | A deep copy of the builder. Every other method returns the same builder. |

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

| Signature | Description |
| --- | --- |
| `on(col1, op, col2)` | The first ON condition. `col2` may be a `QueryBuilder`, which renders as a parenthesized subquery. |
| `andOn(col1, op, col2)` | An AND condition. Raises `RuntimeError` as the first call. |
| `orOn(col1, op, col2)` | An OR condition. Raises `RuntimeError` as the first call. |

A lambda that adds no condition raises `RuntimeError` at render time.

## Combining queries

| Signature | Description |
| --- | --- |
| `union(*queries, all=False)` | UNION, or UNION ALL when `all=True`. |
| `unionAll(*queries)` | UNION ALL. |
| `intersect(*queries)` | INTERSECT. |
| `except_(*queries)` | EXCEPT. The trailing underscore keeps the name off the Python keyword. |

`ORDER BY`, `LIMIT`, and `OFFSET` on the outer query apply to the whole result. The same clauses on a member query render inside that member's parentheses. CTEs from every member hoist to one top-level `WITH`.

## Writing data

| Signature | Description |
| --- | --- |
| `insert(values)` | INSERT from a dict or a list of dicts. Raises `ValueError` on an empty list, on a row with no columns, and on rows whose columns differ from each other. |
| `insert_from(columns, query)` | `INSERT ... SELECT`. `columns=None` inserts positionally. Raises `TypeError` when `query` is not a `QueryBuilder`. |
| `update(values)` | UPDATE. Raises `ValueError` on an empty dict, and at render time when the query has no `where()`. |
| `delete()` | DELETE. Raises `ValueError` at render time when the query has no `where()`. |
| `onConflict(*columns)` | Declares the upsert conflict target. Raises `ValueError` when it does not follow `insert()`, when it gets no columns, and when a conflict column was not one of the inserted columns. |
| `merge(columns=None)` | Updates the row on conflict. Defaults to every inserted column that is not a conflict column. Raises `ValueError` without `onConflict()`, and at render time when no column is left to update. |
| `ignore()` | Skips the row on conflict. Raises `ValueError` without `onConflict()`. |
| `returning(*columns)` | RETURNING. Defaults to `*`. The statement then returns dicts instead of a row count. |
| `create_table_as(table_name, temporary=False)` | CREATE TABLE AS from a SELECT. Raises `ValueError` on a statement that is not a SELECT, and on an empty table name. |

An `UPDATE` or `DELETE` with no filter raises instead of touching every row. To update or delete every row on purpose, add `where(QueryBuilder.raw('1'), '=', 1)`.

Dialect refusals: upserts raise on Presto; `RETURNING` raises on MSSQL, Presto, and Athena; `create_table_as` raises on MSSQL, and on Athena when `temporary=True`.

## Rendering and execution

| Signature | Returns | Description |
| --- | --- | --- |
| `to_sql()` | `(sql, params)` | The parameterized statement, and its values in SQL order. |
| `run(connection=None)` | models, `int`, or dicts | Executes the statement. A SELECT returns hydrated instances with eager relations attached. A write commits, unless it runs inside a `transaction()` block, and returns the affected row count, or dicts when `RETURNING` is set. |
| `first(connection=None)` | `Model` or `None` | Runs a clone of the builder with `LIMIT 1`. The original builder is unchanged. |
| `to_dicts(connection=None)` | `list[dict]` | Rows keyed by column name, with no hydration and no eager loading. |
| `to_df(connection=None)` | `pandas.DataFrame` | Raises `RuntimeError` when pandas is not installed. |
| `to_arrow(connection=None)` | `pyarrow.Table` | Raises `RuntimeError` when pyarrow is not installed. |
| `total(connection=None)` | `int` | `SELECT COUNT(*)` over the query with the ordering, LIMIT, OFFSET, and TOP stripped. The builder is unchanged. |
| `explain(connection=None, analyze=False)` | `list[tuple]` | The query plan. `analyze=True` executes the statement. Raises `DialectError` on MSSQL. |
| `withGraphFetched(*relation_names)` | builder | Eager loads relations, with one query per relation per level. A name may be a dotted path such as `'shows.tickets'`. Raises `ValueError` for an unknown relation name or path segment. |

`to_dicts`, `to_df`, `to_arrow`, and `total` raise `ValueError` on any statement that is not a SELECT.

A multi-row `insert()` with no RETURNING runs through the driver's `executemany()`.

### Async

| Signature | Returns |
| --- | --- |
| `await arun(adapter=None)` | The same shapes as `run()` |
| `await afirst(adapter=None)` | `Model` or `None` |
| `await ato_dicts(adapter=None)` | `list[dict]` |

The adapter resolves in this order: the `adapter` argument, then an open `async_transaction()`, then `Model.bind_async()`. When none of the three resolves, the call raises `RuntimeError`.

## Connection resolution

Every execution method resolves a connection the same way: the `connection` argument first, then a connection pinned by an open `transaction()` block, then the binding from `Model.bind()`. When none of the three resolves, the call raises `RuntimeError`.
