---
layout: default
title: Building Queries
description: "Build SQL in Python with the Sustained query builder: selects, joins, subqueries, CTEs, unions, window functions, CASE, inserts, updates, and upserts."
---

`Model.query()` returns a `QueryBuilder`. Chain query methods onto it to build the statement:

```python
print(
    Show.query()
        .select('title')
        .where('sold_out', '=', True)
        .orderBy('starts_at')
)
# SELECT title FROM shows WHERE sold_out = TRUE ORDER BY starts_at ASC
```

The chain mutates the builder in place. If you want to branch several queries from one base query, make a deep copy with [`clone()`](#reusing-a-query) first.

The examples use the venue booking schema from [Getting Started](./getting-started).

## Where the rows come from

The model's table is the default source, and `from_()` overrides it:

```python
Show.query().from_('shows_archive')
# SELECT * FROM shows_archive

Show.query().from_('shows', 'r')
# SELECT * FROM shows AS r
```

The source can also be another query, which renders as a derived table. You must pass an alias there, because SQL requires a name for every derived table:

```python
sellouts = Show.query().select('id', 'title').where('sold_out', '=', True)

Show.query().from_(sellouts, 'sellouts').select('*')
# SELECT * FROM (SELECT id, title FROM shows WHERE sold_out = TRUE) AS sellouts
```

## Choosing columns

`select()` takes any number of column names. Without it, the query selects everything:

```python
Show.query().select('id', 'title', 'starts_at')
# SELECT id, title, starts_at FROM shows
```

Once a query joins, two tables can both have columns with the same name. Model attributes give you the qualified form without writing the table name twice:

```python
Show.query().select(Show.title, Venue.name).innerJoinRelated('venue')
# SELECT shows.title, venues.name FROM shows INNER JOIN venues ON shows.venue_id = venues.id
```

To alias a column, use the `'column AS alias'` shorthand, and Sustained quotes both halves for the dialect:

```python
Venue.query().select('name AS venue_name')
# SELECT name AS venue_name FROM venues
```

When a join selects the same column name from two tables, alias one of them. A row is keyed by column name, so one value would otherwise replace the other, and Sustained refuses that result set with `AmbiguousColumns`, naming the repeated columns:

```python
Show.query().select(Show.id, Venue.id).innerJoinRelated('venue').run()
# AmbiguousColumns: This result set returns 'id' more than once ...

Show.query().select(Show.id, 'venues.id AS venue_id').innerJoinRelated('venue').run()
```

`distinct()` adds the keyword to the whole select list:

```python
Venue.query().distinct().select('city')
# SELECT DISTINCT city FROM venues
```

### Aggregates

`count()`, `sum()`, `avg()`, `min()`, and `max()` each add one aggregate to the select list. Called with no column, `count()` counts rows:

```python
Show.query().count()
# SELECT COUNT(*) FROM shows

Ticket.query().count('id', alias='sold')
# SELECT COUNT(id) AS sold FROM tickets

Ticket.query().sum('price', alias='gross')
# SELECT SUM(price) AS gross FROM tickets
```

For an aggregate with no method of its own, build an `AggregateExpression` and pass it to `select()`:

```python
from sustained.expressions import AggregateExpression

Artist.query().select(AggregateExpression('STRING_AGG', "name, ', '"))
# SELECT STRING_AGG(name, ', ') FROM artists
```

The argument to `AggregateExpression` is raw SQL, so any dialect-specific quoting is yours to write. [Grouping](./grouping) covers grouping these aggregates and filtering the groups.

### Functions

`select_func()` calls any SQL function and treats each string argument as a column name. To pass a string as data, wrap it in `Literal`, and to pass it as raw SQL, wrap it in `Column`:

```python
from sustained import Literal

Venue.query().select_func('COALESCE', 'name', Literal('unknown'), alias='label')
# SELECT COALESCE(name, 'unknown') AS label FROM venues
```

A string argument that is not a plain column name raises `ValueError` when the query renders:

```python
Venue.query().select_func('COALESCE', 'not a column', alias='x')
# ValueError: Function argument 'not a column' is not a column name.
# Wrap literal values in Literal() or raw SQL in Column().
```

The rule exists because a forgotten `Literal` would turn a value into a column reference, and the query would return the wrong rows instead of failing.

An argument can be another expression: a nested `Func`, an aggregate, a window call, or a `Subquery`. A subquery argument renders through the statement, so under `to_sql()` its values become placeholders and join the outer parameter tuple in the order they appear in the SQL. It renders without its alias, because a function argument takes a bare SELECT:

```python
from sustained.expressions import Subquery

quota = Venue.query().select('capacity').where('id', '=', 3)

Show.query().select_func('COALESCE', Subquery(quota, 'q'), Literal(0), alias='cap')
# SELECT COALESCE((SELECT capacity FROM venues WHERE id = ?), 0) AS cap FROM shows
```

Every registered function also has a named method equivalent, so these build the same query:

```python
Venue.query().select_func('COALESCE', 'name', Literal('unknown'), alias='label')
Venue.query().coalesce('name', Literal('unknown'), alias='label')
```

The registry covers the scalar functions `LOWER`, `UPPER`, `COALESCE`, `CONCAT`, `SUBSTRING`, `TRIM`, `LENGTH`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `MOD`, `NOW`, and `GETDATE`, plus the aggregates `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, and `STRING_AGG`. A registered function checks itself against the configured dialect and raises `DialectError` at build time when the engine has no spelling for it:

```python
from sustained.dialects import Dialects

Venue.set_dialect(Dialects.MSSQL)
Venue.query().select_func('STRING_AGG', 'name')
# DialectError: Function 'STRING_AGG' is not supported by the 'MSSQL' dialect.
```

Some registered names change spelling instead of raising, because the engine spells the same function differently:

```python
Venue.query().length('name', alias='n')
# DEFAULT:  SELECT LENGTH(name) AS n FROM venues
# MSSQL:    SELECT LEN([name]) AS [n] FROM [venues]

Venue.query().now(alias='t')
# POSTGRES: SELECT NOW() AS "t" FROM "venues"
# MSSQL:    SELECT GETDATE() AS [t] FROM [venues]
# DEFAULT:  DialectError: Function 'NOW' is not supported by the 'DEFAULT' dialect.
```

An unregistered name passes through unchecked, so you can call a function the registry does not list:

```python
Venue.query().select_func('SOME_CUSTOM_FN', 'name')
# SELECT SOME_CUSTOM_FN(name) FROM venues
```

The [function reference](./reference/predicates#function-registry) lists every registered name with its per-dialect spelling.

### Window functions

`select_window()` takes the function name, an alias, and the partition and order columns:

```python
Ticket.query().select_window(
    'ROW_NUMBER', 'seat', partition_by=['show_id'], order_by=['sold_at']
)
# SELECT ROW_NUMBER() OVER (PARTITION BY show_id ORDER BY sold_at) AS seat FROM tickets
```

Filtering on the result needs a wrapping subquery on most engines, or [`qualify()`](#analyst-clauses) on DuckDB.

### CASE expressions

`select_case()` takes the alias, the `ELSE` value, and the `WHEN` pairs, and it treats a string in the result position as a literal:

```python
Venue.query().select_case(
    'size',
    'small',
    when_clauses=[
        ('capacity > 5000', 'arena'),
        ('capacity > 1000', 'theatre'),
    ],
)
# SELECT CASE WHEN capacity > 5000 THEN 'arena'
#             WHEN capacity > 1000 THEN 'theatre'
#             ELSE 'small' END AS size
# FROM venues
```

Wrap a result in `Column` when it names a column rather than a value:

```python
from sustained.expressions import Column

Ticket.query().select_case(
    'charged',
    Column('price'),
    when_clauses=[('refunded_at IS NOT NULL', '0.00')],
)
# SELECT CASE WHEN refunded_at IS NOT NULL THEN '0.00' ELSE price END AS charged
# FROM tickets
```

The condition half of each pair is raw SQL and renders as written. A result that is neither a string nor a `Column` raises `TypeError` when the query renders.

### Subqueries in the select list

`Subquery` embeds a whole query as one column. Reference the outer query's columns with `QueryBuilder.raw()`, which keeps Sustained from treating the name as a value:

```python
from sustained.builder import QueryBuilder
from sustained.expressions import Subquery

sold = Ticket.query().count().where('show_id', '=', QueryBuilder.raw('shows.id'))

Show.query().select('title', Subquery(sold, 'tickets_sold'))
# SELECT title, (SELECT COUNT(*) FROM tickets WHERE show_id = shows.id) AS tickets_sold
# FROM shows
```

The inner query renders like any other part of the statement. Under `to_sql()` its values become placeholders and join the outer parameter tuple, in the order they appear in the SQL.

## Ordering

`orderBy()` takes a column and an optional `'asc'` or `'desc'`, defaulting to ascending. Call it once per sort key, in order:

```python
Show.query().orderBy('starts_at', 'desc').orderBy('title')
# SELECT * FROM shows ORDER BY starts_at DESC, title ASC
```

On a query built with `union()`, the ordering applies to the combined result.

## Limiting and paging

`limit()` and `offset()` each take a non-negative integer, and you can call each one once per query:

```python
Show.query().orderBy('starts_at', 'desc').limit(10).offset(5)
# SELECT * FROM shows ORDER BY starts_at DESC LIMIT 10 OFFSET 5
```

`page()` computes the same thing from a zero-based page number and a page size:

```python
Show.query().page(2, 25)
# SELECT * FROM shows LIMIT 25 OFFSET 50
```

`top()` is the T-SQL spelling and puts the limit at the front of the statement:

```python
Show.query().top(10)
# MSSQL:   SELECT TOP 10 * FROM shows
# others:  DialectError: TOP is not supported by the 'DEFAULT' dialect. Use limit() instead.
```

`limit()` and `top()` on the same query raise `ValueError`. On MSSQL, `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL only allows after an `ORDER BY`, so the query raises `DialectError` without one. On Presto, `OFFSET` renders before `LIMIT`. An `offset()` with no `limit()` needs a row limit on the dialects that reject a bare `OFFSET`: the default dialect renders `LIMIT -1 OFFSET n`, which SQLite reads as all rows, and MySQL renders its own all-rows limit. Postgres and DuckDB keep the bare `OFFSET`.

An offset deep into a large table costs a scan that grows with the offset, because the database still walks the skipped rows. `cursor_page()` avoids that scan:

```python
first = Ticket.query().cursor_page('id', 100).run()
next_page = Ticket.query().cursor_page('id', 100, after=first[-1].id).run()
```

`cursor_page()` orders by the column, filters for rows past the last value you saw, and limits to the page size. The column has to be unique and sorted the same way each call, which usually means the primary key.

`total()` runs `SELECT COUNT(*)` over the query with `ORDER BY`, `LIMIT`, and `OFFSET` stripped, and returns the number without changing the builder, which gives you the row count behind a paged query.

## Common table expressions

`with_()` takes an alias and a query. The trailing underscore keeps the method name from clashing with Python's `with` keyword:

```python
big_venues = Venue.query().select('id').where('capacity', '>', 5000)

(Show.query()
    .with_('big_venues', big_venues)
    .join('big_venues', 'shows.venue_id', '=', 'big_venues.id')
    .select('shows.title'))
# WITH big_venues AS (SELECT id FROM venues WHERE capacity > 5000)
# SELECT shows.title FROM shows
# JOIN big_venues ON shows.venue_id = big_venues.id
```

Sustained quotes the CTE alias the way it quotes a table name. On a dialect where quoted names keep their case, such as Postgres, `from_('Recent')` and `join('Recent', ...)` then find a CTE named `Recent`. An alias takes letters, digits, and underscores, and any other string raises `ValueError`, the same as the alias on `from_()`, a relation join, or a `Subquery`.

A subquery can define its own CTE, such as the query you pass to `whereIn()`, `whereExists()`, a `Subquery`, or a join condition. Sustained moves that CTE into the one `WITH` clause at the top of the outer `SELECT`, because MSSQL refuses a `WITH` inside parentheses. Two different subqueries that define the same alias raise `ValueError`. A subquery inside an `UPDATE` or `DELETE` keeps its own `WITH`.

`recursive=True` renders `WITH RECURSIVE`, except on MSSQL, where T-SQL spells recursive CTEs with plain `WITH`. Sustained does not build the anchor and recursive halves for you, so you write them yourself with `raw()` and a `union()`.

## Combining queries

`union()`, `unionAll()`, `intersect()`, and `except_()` each take any number of builders and combine them with the matching set operator. `union()` removes duplicate rows and `unionAll()` keeps them:

```python
sellouts = Show.query().select('id', 'title').where('sold_out', '=', True)
soon = Show.query().select('id', 'title').where('starts_at', '<', '2026-09-01')

sellouts.union(soon)
# (SELECT id, title FROM shows WHERE sold_out = TRUE)
# UNION
# (SELECT id, title FROM shows WHERE starts_at < '2026-09-01')
```

Each member renders inside its own parentheses and keeps its own `ORDER BY` and `LIMIT`. Clauses added to the query afterwards apply to the combination:

```python
sellouts.union(soon).orderBy('title').limit(20)
# (...) UNION (...) ORDER BY title ASC LIMIT 20
```

Sustained lifts the CTEs from every member query into a single `WITH` at the top of the statement. Two different CTEs that share an alias raise `ValueError`, so give each one its own alias.

`except_()` uses the same trailing underscore as `with_()`, for the same reason.

## Analyst clauses

Only some dialects support these clauses.

`distinctOn(*columns)` works on Postgres and DuckDB. It keeps the first row per group and needs an `orderBy()` on the same leading columns to define which row that is:

```python
Show.query().distinctOn('venue_id').orderBy('venue_id').orderBy('starts_at')
# SELECT DISTINCT ON ("venue_id") * FROM "shows" ORDER BY "venue_id" ASC, "starts_at" ASC
```

`qualify(condition)` works on DuckDB. It filters on a window function without a wrapping subquery and takes a `Predicate` or a raw string:

```python
(Ticket.query()
    .select('show_id')
    .select_window(
        'ROW_NUMBER',
        'rn',
        partition_by=['show_id'],
        order_by=['sold_at'])
    .qualify('rn <= 3'))
# SELECT "show_id", ROW_NUMBER() OVER (PARTITION BY "show_id" ORDER BY "sold_at") AS "rn"
# FROM "tickets" QUALIFY rn <= 3
```

`groupByRollup()`, `groupByCube()`, and `groupByGroupingSets()` produce subtotal rows and multi-grain aggregates. They are covered with the rest of `GROUP BY` in [Grouping](./grouping#subtotals-and-multiple-grains).

`for_update(skip_locked=False, nowait=False)` locks the selected rows for the transaction. It is available on Postgres only, and not in a query that uses a union.

## Reading the execution plan

`explain()` runs the dialect's EXPLAIN and returns the plan rows. `explain(analyze=True)` uses EXPLAIN ANALYZE, which runs the statement for real, so do not point it at a write. On MSSQL, `explain()` raises, because T-SQL has no EXPLAIN statement.

## Reusing a query

Each chained call adds to the same builder, so a shared base query collects every branch's filters. To build several queries from one base, `clone()` each branch first:

```python
base = Show.query().where('sold_out', '=', True)

fillmore = base.clone().where('venue_id', '=', 1)
# SELECT * FROM shows WHERE sold_out = TRUE AND venue_id = 1

first_ave = base.clone().where('venue_id', '=', 2)
# SELECT * FROM shows WHERE sold_out = TRUE AND venue_id = 2
```

Without the clones, the second line would filter on both venues and return nothing.

## Method naming

The canonical names are camelCase: `orderBy`, `groupBy`, `whereIn`, `unionAll`, `leftJoin`. Each also accepts its snake_case spelling: `order_by`, `group_by`, `where_in`, `union_all`, `left_join`. Sustained ignores case and underscores when it matches a name, so `whereILike` is also `where_i_like` or `where_ilike`. (Method names are also not case sensitive, so you can use Mocking Spongebob case if you want to `.iNnErJoIn()` something.)

## Getting the SQL out

`str(query)` renders values inline as SQL literals, for reading and logging:

```python
print(Show.query().select('title').where('id', '=', 1))
# SELECT title FROM shows WHERE id = 1
```

`to_sql()` returns the SQL with placeholders and with the parameters as a separate tuple, in the order they appear.

```python
Show.query().select('title').where('id', '=', 1).to_sql()
# ('SELECT title FROM shows WHERE id = ?', (1,))
```

The placeholder follows the dialect, so it is `?` by default and on MSSQL, and `%s` on Postgres:

```python
Show.set_dialect(Dialects.POSTGRES)
Show.query().select('title').where('id', '=', 1).to_sql()
# ('SELECT "title" FROM "shows" WHERE "id" = %s', (1,))
```

On Postgres and MySQL, `to_sql()` writes every literal `%` sign in the text as `%%`, because psycopg, PyMySQL, and mysqlclient read a bare `%` as the start of a placeholder. The driver reads `%%` back as one `%`, so `whereRaw('price % ? = ?', [10, 0])` and a `Literal('100%')` reach the database as written. `str(query)` keeps the single `%`.

## Where to go next

| You want to | Read |
| --- | --- |
| Narrow the rows | [Filtering](./filtering) |
| Aggregate and filter the groups | [Grouping](./grouping) |
| Bring in a second table | [Relations and Joins](./relations) |
| Run it, write rows, use a transaction | [Executing Queries](./executing) |
| Know what a given engine refuses | [SQL Dialects](./dialects) |
| Look up a method exactly | [QueryBuilder reference](./reference/query-builder) |
