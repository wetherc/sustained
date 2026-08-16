---
layout: default
title: Grouping and Having Clauses
---

`groupBy()` collapses rows, and `having()` filters what is left:

```python
(Ticket.query()
    .select('show_id')
    .count('id', alias='sold')
    .groupBy('show_id')
    .having('COUNT(id)', '>', 100))
# SELECT show_id, COUNT(id) AS sold FROM tickets GROUP BY show_id HAVING COUNT(id) > 100
```

WHERE runs before the grouping and HAVING runs after, so a filter on a raw
column belongs in `where()` and a filter on an aggregate belongs in
`having()`. Putting an aggregate in `where()` is an error in the database, not
in Sustained.

The examples use the venue booking schema from
[Getting Started](./getting-started).

## Grouping rows

`groupBy()` takes any number of columns:

```python
Ticket.query().groupBy('show_id')
# SELECT * FROM tickets GROUP BY show_id

Show.query().groupBy('venue_id', 'sold_out')
# SELECT * FROM shows GROUP BY venue_id, sold_out
```

Pair it with an aggregate from [Queries](./queries#aggregates), which is what
makes the grouping do any work:

```python
(Ticket.query()
    .select('show_id')
    .sum('price', alias='gross')
    .groupBy('show_id')
    .orderBy('gross', 'desc'))
# SELECT show_id, SUM(price) AS gross FROM tickets GROUP BY show_id ORDER BY gross DESC
```

## Filtering groups

`having()`, `andHaving()`, and `orHaving()` chain the same way their `where`
counterparts do, and take the same three arguments:

```python
(Ticket.query()
    .groupBy('show_id')
    .having('COUNT(id)', '>', 100)
    .andHaving('SUM(price)', '>', 5000))
# SELECT * FROM tickets GROUP BY show_id HAVING COUNT(id) > 100 AND SUM(price) > 5000

(Ticket.query()
    .groupBy('show_id')
    .having('AVG(price)', '<', 25)
    .orHaving('MAX(price)', '>', 250))
# SELECT * FROM tickets GROUP BY show_id HAVING AVG(price) < 25 OR MAX(price) > 250
```

The first condition in a chain must be a plain `having()`. Starting with
`andHaving()` or `orHaving()` raises `RuntimeError`.

Write the aggregate as it appears in the source, not as its alias. Standard
SQL evaluates HAVING before the SELECT list exists, so `having('gross', '>',
5000)` is rejected by the database even when `gross` is right there in the
select list:

```python
# Works everywhere.
Ticket.query().select('show_id').sum('price', alias='gross') \
    .groupBy('show_id').having('SUM(price)', '>', 5000)
```

## The having family

The `having` methods are generated from the same twelve bases as the `where`
methods, in the same plain, `and`, and `or` forms. Every method in
[the where family](./filtering#the-where-family) has a `having` twin with
identical arguments and identical errors:

| Method | Arguments |
| --- | --- |
| `having` | column, operator, value |
| `havingIn` / `havingNotIn` | column, list or query |
| `havingBetween` / `havingNotBetween` | column, low, high |
| `havingLike` / `havingILike` | column, pattern |
| `havingNull` / `havingNotNull` | column |
| `havingExists` / `havingNotExists` | query or callable |
| `havingRaw` | SQL, parameters |

```python
Show.query().groupBy('venue_id').havingIn('venue_id', [1, 2, 3])
# SELECT * FROM shows GROUP BY venue_id HAVING venue_id IN (1, 2, 3)

Venue.query().groupBy('city').havingLike('city', 'Minne%')
# SELECT * FROM venues GROUP BY city HAVING city LIKE 'Minne%'

Ticket.query().groupBy('show_id').havingRaw('SUM(price) % ? = ?', [10, 0])
# SELECT * FROM tickets GROUP BY show_id HAVING (SUM(price) % 10 = 0)
```

`havingILike()` follows the same rule as `whereILike()`: native `ILIKE` on
Postgres and DuckDB, a lowercased comparison everywhere else.

## Grouping conditions

Pass a callable to any `having` method for a parenthesized group. The builder
it receives has the `having` methods on it:

```python
(Ticket.query()
    .groupBy('show_id')
    .having('COUNT(id)', '>', 50)
    .andHaving(lambda q: (
        q.having('SUM(price)', '<', 1000).orHaving('MAX(price)', '>=', 250)
    )))
# SELECT * FROM tickets GROUP BY show_id
# HAVING COUNT(id) > 50 AND (SUM(price) < 1000 OR MAX(price) >= 250)
```

Groups nest as deep as the logic needs. Past two levels, a typed predicate
built from `Model.c` and combined with `&` and `|` reads better, and
`having()` accepts one in place of the three arguments.

## Subtotals and multiple grains

Three methods produce the grouping sets that give you subtotal rows alongside
the detail. Each raises `ValueError` when called with no columns, and
`DialectError` on an engine that lacks the syntax.

`groupByRollup()` adds a subtotal for each prefix of the column list, ending
in a grand total. Ordering matters here, because a rollup is a hierarchy:

```python
Ticket.query().select('price').groupByRollup('show_id')
# SELECT price FROM tickets GROUP BY ROLLUP (show_id)
```

`groupByCube()` adds a subtotal for every combination of the columns, so
ordering does not matter and the row count grows as a power of two.

`groupByGroupingSets()` takes the combinations explicitly, as tuples. An empty
tuple is the grand total:

```python
Ticket.query().groupByGroupingSets(('show_id',), ('show_id', 'sold_at'), ())
```

Rows produced by a subtotal carry `NULL` in the columns they aggregate over,
which is indistinguishable from a real `NULL` in the data. Engines expose a
`GROUPING()` function to tell them apart; reach it with `select_func()`.

## Where to go next

| You want to | Read |
| --- | --- |
| Filter rows before the grouping | [Filtering](./filtering) |
| Build the aggregates being grouped | [Queries](./queries#aggregates) |
| Rank inside a group instead of collapsing it | [Queries](./queries#window-functions) |
| Run the query | [Executing Queries](./executing) |
| See every method and what it raises | [QueryBuilder reference](./reference/query-builder#grouping-and-window-filters) |
