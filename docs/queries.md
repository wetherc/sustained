---
layout: default
title: Building Queries
---

`Model.query()` returns a `QueryBuilder`. Query methods are chained onto this to build out a full query:

```python
print(
    Show.query()
        .select('title')
        .where('sold_out', '=', True)
        .orderBy('starts_at')
)
# SELECT title FROM shows WHERE sold_out = TRUE ORDER BY starts_at ASC
```

The chain mutates the builder in place. If you want to run multiple branching queries off of the same base query, you must make a deep copy with [`clone()`](#reusing-a-query) first.

This page continues to use the venue booking schema from [Getting Started](./getting-started).

## Where the rows come from

The model's table is the default source, but can be overridden with `from_()`:

```python
Show.query().from_('shows_archive')
# SELECT * FROM shows_archive

Show.query().from_('shows', 'r')
# SELECT * FROM shows AS r
```

The source can also be another query, which renders as a derived table. An alias is required there, because SQL has no name for an unnamed subquery:

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

An alias uses the `'column AS alias'` shorthand. Both halves quote correctly for the dialect:

```python
Venue.query().select('name AS venue_name')
# SELECT name AS venue_name FROM venues
```

Alias the second one when a join selects the same column name from two tables. A row is keyed by column name, so one value would replace the other. Sustained refuses that result set with `AmbiguousColumns` and names the repeated columns:

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

`count()`, `sum()`, `avg()`, `min()`, and `max()` each add one aggregate to the select list. Called with no column specified, `count()` counts rows:

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

The argument to `AggregateExpression` is raw SQL, so you do need to get the dialect-specific quoting right there. Grouping those aggregates and filtering the groups is detailed in [Grouping](./grouping).

### Functions

`select_func()` allows you to call any SQL function. String arguments are interpreted as column names. You can wrap a string value in `Literal` to pass it as data, and in `Column` to pass it as raw SQL:

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

That rule exists because without it, a forgotten `Literal` turns a value into a column reference and the query silently returns the wrong rows rather than failing.

Every registered function also has a named method equivalent, so these build the same query:

```python
Venue.query().select_func('COALESCE', 'name', Literal('unknown'), alias='label')
Venue.query().coalesce('name', Literal('unknown'), alias='label')
```

The registry holds the scalar functions `LOWER`, `UPPER`, `COALESCE`, `CONCAT`, `SUBSTRING`, `TRIM`, `LENGTH`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `MOD`, `NOW`, and `GETDATE`, plus the aggregates `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, and `STRING_AGG`. A registered function checks itself against the configured dialect and raises `DialectError` at build time when the engine has no spelling for it:

```python
from sustained.dialects import Dialects

Venue.set_dialect(Dialects.MSSQL)
Venue.query().select_func('STRING_AGG', 'name')
# DialectError: Function 'STRING_AGG' is not supported by the 'MSSQL' dialect.
```

Some registered names change spelling instead of raising to account for dialect-specific variations in function names. For example:

```python
Venue.query().length('name', alias='n')
# DEFAULT:  SELECT LENGTH(name) AS n FROM venues
# MSSQL:    SELECT LEN([name]) AS [n] FROM [venues]

Venue.query().now(alias='t')
# POSTGRES: SELECT NOW() AS "t" FROM "venues"
# MSSQL:    SELECT GETDATE() AS [t] FROM [venues]
# DEFAULT:  DialectError: Function 'NOW' is not supported by the 'DEFAULT' dialect.
```

An unregistered name passes through unchecked, which is how you reach a function Sustained has never heard of:

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

`select_case()` takes the alias, the `ELSE` value, and the `WHEN` pairs. Strings in the result position are literals:

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

`Subquery` embeds a whole query as one column. Reference the outer query's columns with `QueryBuilder.raw()`, which stops the name being treated as a value:

```python
from sustained.builder import QueryBuilder
from sustained.expressions import Subquery

sold = Ticket.query().count().where('show_id', '=', QueryBuilder.raw('shows.id'))

Show.query().select('title', Subquery(sold, 'tickets_sold'))
# SELECT title, (SELECT COUNT(*) FROM tickets WHERE show_id = shows.id) AS tickets_sold
# FROM shows
```

## Ordering

`orderBy()` takes a column and an optional `'asc'` or `'desc'`, defaulting to ascending. Call it once per sort key, in order:

```python
Show.query().orderBy('starts_at', 'desc').orderBy('title')
# SELECT * FROM shows ORDER BY starts_at DESC, title ASC
```

On a query built with `union()`, the ordering applies to the combined result.

## Limiting and paging

`limit()` and `offset()` are paired methods. Each takes a non-negative
integer and can be called once:

```python
Show.query().orderBy('starts_at', 'desc').limit(10).offset(5)
# SELECT * FROM shows ORDER BY starts_at DESC LIMIT 10 OFFSET 5
```

`page()` computes the same thing from a zero-based page number and a page size:

```python
Show.query().page(2, 25)
# SELECT * FROM shows LIMIT 25 OFFSET 50
```

`top()` is the T-SQL spelling, and puts the limit at the front of the statement:

```python
Show.query().top(10)
# MSSQL:   SELECT TOP 10 * FROM shows
# others:  DialectError: TOP is not supported by the 'DEFAULT' dialect. Use limit() instead.
```

`limit()` and `top()` on the same query raise `ValueError`. On MSSQL, `limit()` and `offset()` compile to `OFFSET ... FETCH`, which T-SQL only allows after an `ORDER BY`, so the query raises `DialectError` without one. On Presto, `OFFSET` renders before `LIMIT`. An `offset()` with no `limit()` needs a row cap on the dialects that reject a bare `OFFSET`: the default dialect renders `LIMIT -1 OFFSET n`, which SQLite reads as all rows, and MySQL renders its own all-rows cap. Postgres and DuckDB keep the bare `OFFSET`. And an offset deep into a large table costs a scan that grows with the offset. To avoid this, use `cursor_page()`:

```python
first = Ticket.query().cursor_page('id', 100).run()
next_page = Ticket.query().cursor_page('id', 100, after=first[-1].id).run()
```

`cursor_page()` orders by the column, filters for rows past the last value you saw, and limits to the page size. The column has to be unique and sorted the same way each call, which usually means the primary key.

`total()` runs `SELECT COUNT(*)` over the query with `ORDER BY`, `LIMIT`, and `OFFSET` stripped, and returns the number without changing the builder. It is the row count a paged query would have had.

## Common table expressions

`with_()` takes an alias and a query. The trailing underscore is needed to distinguish the method from Python's `with` keyword:

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

`recursive=True` renders `WITH RECURSIVE`, except on MSSQL, where T-SQL spells recursive CTEs with plain `WITH`. Sustained does not build the anchor and recursive halves for you; you will need to write that with `raw()` and a `union()` yourself.

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

CTEs from every member query are elevated to a single `WITH` at the top of the statement. Two different CTEs sharing an alias raise `ValueError` and must be disambiguated.

`except_()` uses the same trailing underscore as `with_()`, for the same reason.

## Analyst clauses

These clauses are only supported for a subset of the available dialects.

`distinctOn(*columns)` keeps the first row per group, and needs an `orderBy()` on the same leading columns to define which row that is. For Postgres and DuckDB:

```python
Show.query().distinctOn('venue_id').orderBy('venue_id').orderBy('starts_at')
# SELECT DISTINCT ON ("venue_id") * FROM "shows" ORDER BY "venue_id" ASC, "starts_at" ASC
```

`qualify(condition)` filters on a window function without a wrapping subquery. It takes a `Predicate` or a raw string. For DuckDB:

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

`for_update(skip_locked=False, nowait=False)` locks the selected rows for the transaction. Postgres only, and unavailable in queries using unions.

## Reading the execution plan

`explain()` runs the dialect's EXPLAIN and returns the plan rows. `explain(analyze=True)` uses EXPLAIN ANALYZE, which runs the statement for real, so do not point it at a write. MSSQL raises, because T-SQL has no EXPLAIN statement.

## Reusing a query

Each chained call adds to the same builder, so a shared base query collects every branch's filters. `clone()` copies it if you want to create multiple branch queries from the same base:

```python
base = Show.query().where('sold_out', '=', True)

fillmore = base.clone().where('venue_id', '=', 1)
# SELECT * FROM shows WHERE sold_out = TRUE AND venue_id = 1

first_ave = base.clone().where('venue_id', '=', 2)
# SELECT * FROM shows WHERE sold_out = TRUE AND venue_id = 2
```

Without the clones, the second line would filter on both venues and return nothing.

## Method naming

The canonical names are camelCase: `orderBy`, `groupBy`, `whereIn`, `unionAll`, `leftJoin`. Each also accepts its snake_case spelling: `order_by`, `group_by`, `where_in`, `union_all`, `left_join`. The translation is mechanical, and it uppercases the letter after each underscore, so `whereILike` is `where_i_like`. (Method names are also not case sensitive, so you could probably use Mocking Spongebob case if you wanted to `.iNnErJoIn()` something. Note that at time of writing I haven't actually tested this, but am pretty sure it holds.)

## Getting the SQL out

`str(query)` renders values inline as SQL literals. It is for reading and logging:

```python
print(Show.query().select('title').where('id', '=', 1))
# SELECT title FROM shows WHERE id = 1
```

`to_sql()` returns the SQL with placeholders and with the parameters as a separate tuple, in the order they appear.

```python
Show.query().select('title').where('id', '=', 1).to_sql()
# ('SELECT title FROM shows WHERE id = ?', (1,))
```

The placeholder follows the dialect, `?` by default and on MSSQL, `%s` on Postgres:

```python
Show.set_dialect(Dialects.POSTGRES)
Show.query().select('title').where('id', '=', 1).to_sql()
# ('SELECT "title" FROM "shows" WHERE "id" = %s', (1,))
```

## Where to go next

| You want to | Read |
| --- | --- |
| Narrow the rows | [Filtering](./filtering) |
| Aggregate and filter the groups | [Grouping](./grouping) |
| Bring in a second table | [Relations and Joins](./relations) |
| Run it, write rows, use a transaction | [Executing Queries](./executing) |
| Know what a given engine refuses | [SQL Dialects](./dialects) |
| Look up a method exactly | [QueryBuilder reference](./reference/query-builder) |
