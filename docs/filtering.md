---
layout: default
title: Filtering Queries
---

There are two syntaxes you can use to write a condition:

```python
Show.query().where('sold_out', '=', True)
# SELECT * FROM shows WHERE sold_out = TRUE

Show.query().where(Show.c.sold_out == True)
# SELECT * FROM shows WHERE shows.sold_out = TRUE
```

The three-argument form takes a column name, an operator, and a value, and renders the name exactly as written. The typed form uses Python's own comparison operators against `Model.c`, checks the name against the model's declared
columns, and qualifies it with the table. Strings are quicker to type and work on any column, including ones not explicitly declaured on a model. Typed predicates combine with `&`, `|`, and `~`, which is what you want for more complicated compound and nested where clauses.

The examples on this page use the venue booking schema from [Getting Started](./getting-started). Either syntax can be used interchangeably throughout the examples.

## Comparisons

`where()`, `andWhere()`, and `orWhere()` chain to build the clause:

```python
Show.query().where('title', '=', 'Nightcrawler')
# SELECT * FROM shows WHERE title = 'Nightcrawler'

Venue.query().where('capacity', '>', 1000).andWhere('city', '=', 'Minneapolis')
# SELECT * FROM venues WHERE capacity > 1000 AND city = 'Minneapolis'

Venue.query().where('city', '=', 'Minneapolis').orWhere('city', '=', 'St Paul')
# SELECT * FROM venues WHERE city = 'Minneapolis' OR city = 'St Paul'
```

The first condition in a chain must be a plain `where()`. Starting with `andWhere()` or `orWhere()` raises `RuntimeError`, because there is nothing for the conjunction to join to.

The operator has to be one of `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`, `ILIKE`, `NOT ILIKE`, `IS`, or `IS NOT`. Anything else raises `ValueError`. The allowlist exists because the operator is the one part of the clause that will always render exactly as written; use `whereRaw()` if you need more flexibility.

Comparing to `None` with `=` or `!=` renders `IS NULL` or `IS NOT NULL`, because `= NULL` matches nothing in SQL:

```python
Show.query().where('starts_at', '=', None)
# SELECT * FROM shows WHERE starts_at IS NULL
```

`None` with any other operator raises `ValueError`.

## Typed predicates

`Model.c` gives every column a reference that builds a `Predicate` from a Python comparison. `col()` does the same for a dotted path when no model is in scope:

```python
from sustained import col

Venue.query().where((Venue.c.capacity > 1000) & (Venue.c.city == 'Minneapolis'))
# SELECT * FROM venues WHERE (venues.capacity > 1000 AND venues.city = 'Minneapolis')

Show.query().innerJoinRelated('venue').where(col('venues.capacity') > 1400)
```

You can combine predicates with `&` for AND, `|` for OR, and `~` for NOT. Python's precedence rules apply, so parenthesize each comparison:

```python
Show.query().where(
    ((Show.c.sold_out == True) & (Show.c.venue_id == 1)) |
    ~(Show.c.title.like('Cancelled%'))
)
```

Use `&` and `|`, never the `and` and `or` keywords. Python evaluates those by truthiness, which would silently discard half your condition, so a `Predicate` raises `TypeError` in a boolean context instead.

The following builtin methods are also provided for convenience:

```python
Show.c.title.like('The %')
Show.c.title.not_like('The %')

Show.c.title.ilike('the %')

Show.c.venue_id.in_([1, 2, 3])
Show.c.venue_id.not_in([1, 2, 3])

Ticket.c.price.between(20, 50)
Ticket.c.price.not_between(20, 50)

Show.c.starts_at.is_null()
Show.c.starts_at.not_null()
```

A predicate goes to `where()` or to `having()`, and works anywhere the three-argument form does.

## The where family

Each of the following methods can be used as-is, or can be prefixed with `and` or `or`. I.e., `whereBetween` can also be written as `andWhereBetween` and `orWhereBetween` for more complicated conditional statements.

| Method | Arguments | Renders |
| --- | --- | --- |
| `where` | column, operator, value | `col = ?` |
| `whereIn` / `whereNotIn` | column, list or query | `col IN (...)` |
| `whereBetween` / `whereNotBetween` | column, low, high | `col BETWEEN ? AND ?` |
| `whereLike` / `whereILike` | column, pattern | `col LIKE ?` |
| `whereNull` / `whereNotNull` | column | `col IS NULL` |
| `whereExists` / `whereNotExists` | query or callable | `EXISTS (...)` |
| `whereRaw` | SQL, parameters | the fragment, in parentheses |

The same twelve bases exist as `having` methods for filtering groups. See [Grouping](./grouping).

### IN and NOT IN

```python
Show.query().whereIn('venue_id', [1, 2, 3])
# SELECT * FROM shows WHERE venue_id IN (1, 2, 3)

Show.query().where('sold_out', '=', True).andWhereNotIn('venue_id', [4, 5])
# SELECT * FROM shows WHERE sold_out = TRUE AND venue_id NOT IN (4, 5)
```

An empty list raises `ValueError`. `IN ()` is a syntax error on most engines, and the intent behind an empty list is usually a filter that got no values rather than a query that should match nothing.

The values can be another query instead of a list:

```python
Show.query().whereIn('venue_id', Venue.query().select('id').where('capacity', '>', 5000))
# SELECT * FROM shows WHERE venue_id IN (SELECT id FROM venues WHERE capacity > 5000)
```

### LIKE and case-insensitive LIKE

```python
Show.query().whereLike('title', 'The %')
# SELECT * FROM shows WHERE title LIKE 'The %'
```

`whereILike()` matches without regard to case. On Postgres and DuckDB it renders native `ILIKE`. Everywhere else it compiles to a lowercased comparison, so it never raises `DialectError`:

```python
Show.query().whereILike('title', 'the %')
# POSTGRES: SELECT * FROM "shows" WHERE "title" ILIKE 'the %'
# DEFAULT:  SELECT * FROM shows WHERE LOWER(title) LIKE LOWER('the %')
```

The snake_case spelling of `whereILike` is `where_i_like`, because the translation uppercases the letter after each underscore.

### NULL and BETWEEN

```python
Show.query().whereNull('starts_at')
# SELECT * FROM shows WHERE starts_at IS NULL

Ticket.query().whereBetween('price', 20, 50)
# SELECT * FROM tickets WHERE price BETWEEN 20 AND 50
```

`BETWEEN` is inclusive at both ends on every supported engine.

### EXISTS

`whereExists()` takes a query or a callable that receives one. Reference the outer query's columns with `QueryBuilder.raw()`, which stops the name being rendered as a string value:

```python
from sustained.builder import QueryBuilder

Show.query().whereExists(
    Ticket.query()
    .select(QueryBuilder.raw('1'))
    .where('tickets.show_id', '=', QueryBuilder.raw('shows.id'))
)
# SELECT * FROM shows
# WHERE EXISTS (SELECT 1 FROM tickets WHERE tickets.show_id = shows.id)
```

The callable form builds the subquery in place, which keeps a one-use subquery next to the condition that uses it:

```python
(Show.query()
    .whereNotExists(lambda q: (
        q.from_('tickets')
            .select(QueryBuilder.raw('1'))
            .where('tickets.show_id', '=', QueryBuilder.raw('shows.id'))
            .andWhere('tickets.sold_at', 'IS NOT', None)
    ))
)

# SELECT * FROM shows
# WHERE NOT EXISTS (
#   SELECT 1 FROM tickets
#   WHERE tickets.show_id = shows.id AND tickets.sold_at IS NOT NULL
# )
```

The builder the callable receives has no model behind it, so `from_()` takes a table name string rather than a model class.

## Grouping conditions

Pass a callable to any `where` method and it receives a builder for a parenthesized group. This is how you get `A AND (B OR C)`:

```python
(Show.query()
    .where('sold_out', '=', True)
    .andWhere(
        lambda q: (
            q.where('venue_id', '=', 1).orWhere('venue_id', '=', 2)
        )
    )
)

# SELECT * FROM shows WHERE sold_out = TRUE AND (venue_id = 1 OR venue_id = 2)
```

Groups nest as deep as the logic needs:

```python
(Ticket.query()
    .where('sold_at', 'IS NOT', None)
    .andWhere(
        lambda q: (
            q.where(
                lambda a: a.where('price', '<', 20).andWhere('show_id', '=', 1)
            )
            .orWhere(
                lambda b: b.where('price', '>', 100).andWhere('show_id', '=', 2)
            )
        )
    )
)

# SELECT * FROM tickets
# WHERE sold_at IS NOT NULL
#   AND ((price < 20 AND show_id = 1) OR (price > 100 AND show_id = 2))
```

The typed form does the same thing with parentheses instead of lambdas, and reads better past two levels:

```python
Ticket.query().where(
    Ticket.c.sold_at.not_null() &
    (
        ((Ticket.c.price < 20) & (Ticket.c.show_id == 1)) |
        ((Ticket.c.price > 100) & (Ticket.c.show_id == 2))
    )
)
```

## Raw predicates

`whereRaw(sql, params)` is a fallback for anything that you can't otherwise accomplish with the builder. Mark each value with `?`. The values travel as parameters, so a raw fragment is still not a place where user input reaches the SQL text:

```python
Ticket.query().whereRaw('price % ? = ?', [10, 0])
# SELECT * FROM tickets WHERE (price % 10 = 0)
```

The marker count must match the parameter count, or the call raises `ValueError`. The fragment renders wrapped in parentheses, so it composes with the rest of the chain without precedence surprises. `havingRaw()` is the same method for HAVING.

`whereRaw` is dialect-specific: the fragment is passed through untouched, so it will not follow `set_dialect()` the way the rest of the builder does and you are responsible for ensuring that the raw SQL you supply is syntactically valid for your database engine.

## Where to go next

| You want to | Read |
| --- | --- |
| Filter groups rather than rows | [Grouping](./grouping) |
| Filter across a join | [Relations and Joins](./relations) |
| Run the filtered query | [Executing Queries](./executing) |
| See every method and what it raises | [QueryBuilder reference](./reference/query-builder#filtering) |
| See every operator on a typed column | [Predicates reference](./reference/predicates) |
