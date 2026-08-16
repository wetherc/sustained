---
layout: default
title: Relations and Joins
---

A relation is a join written down once on the model, so the query does not
have to repeat it:

```python
Show.query().select('shows.title', 'venues.name').innerJoinRelated('venue')
# SELECT shows.title, venues.name FROM shows
# INNER JOIN venues ON shows.venue_id = venues.id
```

Relations are also what `withGraphFetched()` loads and what the migrator turns
into foreign keys. A join you write once belongs in `relationMappings`; a join
you need in one query is a [raw join](#raw-joins).

The examples use the venue booking schema from
[Getting Started](./getting-started): venues hold shows, shows sell tickets,
and artists play shows through a `show_artists` link table.

## Declaring a relation

`relationMappings` is a dict from relation name to mapping. Each mapping needs
three keys: the `relation` type, the `modelClass` it points at, and the `join`
that connects the two tables.

```python
from sustained import Model, RelationType

class Show(Model):
    tableName = 'shows'
    relationMappings = {
        'venue': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': 'Venue',
            'join': {'from': 'shows.venue_id', 'to': 'venues.id'},
        },
    }
```

The name on the left is what you pass to `innerJoinRelated('venue')` and
`withGraphFetched('venue')`. It is yours to choose, and it usually reads best
as a noun: `venue` for the one, `shows` for the many.

### Relation types

| Type | Cardinality | Eager loading attaches |
| --- | --- | --- |
| `BelongsToOneRelation` | many to one | one instance or `None` |
| `HasOneRelation` | one to one | one instance or `None` |
| `HasManyRelation` | one to many | a list |
| `ManyToManyRelation` | many to many, through a link table | a list |

The type does not change the SQL a `joinRelated` call produces, apart from the
extra hop a `ManyToManyRelation` makes through its link table. It changes what
eager loading attaches to each instance, and it is what the migrator reads
when it works out which side holds the foreign key.

A `Show` belongs to one `Venue`, and the same relation seen from the other
side is a `HasManyRelation`:

```python
class Venue(Model):
    tableName = 'venues'
    relationMappings = {
        'shows': {
            'relation': RelationType.HasManyRelation,
            'modelClass': 'Show',
            'join': {'from': 'venues.id', 'to': 'shows.venue_id'},
        },
    }
```

Both sides are optional. Declare the direction you query in.

### Relations through a link table

A `ManyToManyRelation` adds a `through` key naming the link table and the two
columns in it. An artist plays many shows, a show has many artists, and
`show_artists` holds the pairs:

```python
class Artist(Model):
    tableName = 'artists'
    relationMappings = {
        'shows': {
            'relation': RelationType.ManyToManyRelation,
            'modelClass': 'Show',
            'join': {
                'from': 'artists.id',
                'through': {
                    'from': {'table': 'show_artists', 'key': 'artist_id'},
                    'to': {'table': 'show_artists', 'key': 'show_id'},
                },
                'to': 'shows.id',
            },
        },
    }
```

Read the four columns as the path the join walks: `artists.id` to
`show_artists.artist_id`, across to `show_artists.show_id`, then to
`shows.id`.

### Naming a model by string

`modelClass` takes the class itself or its name as a string. Every model with
a `tableName` registers under its class name when the class body runs, so the
string form resolves through that registry:

```python
'modelClass': Venue      # the class, when it is already imported
'modelClass': 'Venue'    # the name, resolved when the query is built
```

The string form is what breaks import cycles. `Show` and `Venue` can point at
each other from separate modules, as long as both classes exist by the time
the query builds. A name that never resolves raises `ValueError`, naming the
reference it could not find.

## Joining a relation

Nine methods add a join from a declared relation. The prefix on the name picks
the join type and nothing else:

| Method | Renders |
| --- | --- |
| `joinRelated()` | `JOIN` |
| `innerJoinRelated()` | `INNER JOIN` |
| `leftJoinRelated()` | `LEFT JOIN` |
| `leftOuterJoinRelated()` | `LEFT OUTER JOIN` |
| `rightJoinRelated()` | `RIGHT JOIN` |
| `rightOuterJoinRelated()` | `RIGHT OUTER JOIN` |
| `fullJoinRelated()` | `FULL JOIN` |
| `fullOuterJoinRelated()` | `FULL OUTER JOIN` |
| `crossJoinRelated()` | `CROSS JOIN` |

The keyword renders as named, so `leftJoinRelated()` produces `LEFT JOIN` and
`leftOuterJoinRelated()` produces `LEFT OUTER JOIN`. The two mean the same
thing in SQL.

```python
Show.query().select('shows.title', 'venues.name').leftOuterJoinRelated('venue')
# SELECT shows.title, venues.name FROM shows
# LEFT OUTER JOIN venues ON shows.venue_id = venues.id
```

Once a query joins, prefer qualified column names. `select('name')` is
ambiguous when both tables have one, and the engine will say so.

### Aliasing the joined table

Pass `alias` to name the joined table, which a self-join needs and a long
table name makes pleasant:

```python
Show.query().select('shows.title', 'v.name').innerJoinRelated('venue', alias='v')
# SELECT shows.title, v.name FROM shows
# INNER JOIN venues AS v ON shows.venue_id = v.id
```

The alias replaces the table name on the far side of the ON clause too, so
your `select()` and `where()` must use it.

### What a through join renders

Joining a `ManyToManyRelation` produces two joins. The hop to the link table
is always an INNER JOIN, and the join type you asked for applies to the far
table:

```python
Artist.query().select('artists.name', 'shows.title').leftJoinRelated('shows')
# SELECT artists.name, shows.title FROM artists
# INNER JOIN show_artists ON artists.id = show_artists.artist_id
# LEFT JOIN shows ON show_artists.show_id = shows.id
```

An outer join to the link table would produce rows with a null link and a null
far side, which is the same as no row at all.

## Loading relations instead of joining them

A join flattens the related rows into the same result rows, so a venue with
five shows appears five times. `withGraphFetched()` runs a second query
instead and attaches the results to each instance:

```python
venues = Venue.query().withGraphFetched('shows').run()

for venue in venues:
    print(venue.name, len(venue.shows))
```

One extra query per relation, and each instance keeps its own identity. A
dotted path reaches further, `withGraphFetched('shows.tickets')` for the
tickets of every show of every venue, at one query per level. See
[Executing Queries](./executing#eager-loading-relations) for what it needs in
the select list and which relation types it supports on async adapters.

## Raw joins

For a join with no relation behind it, the raw methods take a table name
directly. There is one per join type: `join`, `innerJoin`, `leftJoin`,
`leftOuterJoin`, `rightJoin`, `rightOuterJoin`, `fullJoin`, `fullOuterJoin`,
and `crossJoin`.

The simplest form is a table and one ON condition, given as three arguments:

```python
Venue.query().leftJoin('shows', 'venues.id', '=', 'shows.venue_id')
# SELECT * FROM venues LEFT JOIN shows ON venues.id = shows.venue_id
```

When the join columns share a name in both tables, `using` is shorter and
produces one merged column instead of two:

```python
Show.query().join('show_artists', using=['show_id'])
# SELECT * FROM shows JOIN show_artists USING (show_id)
```

### Several conditions

Pass a callable in place of the three arguments and it receives a join
builder with `on()`, `andOn()`, and `orOn()`. Each takes two columns and an
operator:

```python
Show.query().join(
    'tickets',
    lambda j: j.on('tickets.show_id', '=', 'shows.id')
               .andOn('tickets.price', '>', 'shows.floor_price'),
)
# SELECT * FROM shows JOIN tickets
# ON tickets.show_id = shows.id AND tickets.price > shows.floor_price
```

Both sides of an `on()` are column references. To compare a column against a
value, put the condition in `where()` instead, or wrap the value in
`QueryBuilder.raw()`.

### A subquery on the right of ON

The right side of an ON condition can be a whole query, which renders
parenthesized. This joins each show to its most expensive ticket:

```python
priciest = Ticket.query().select('MAX(price)').whereRaw('tickets.show_id = shows.id', [])

Show.query().select('shows.title', 'tickets.price').join(
    'tickets',
    lambda j: j.on('tickets.show_id', '=', 'shows.id')
               .andOn('tickets.price', '=', priciest),
)
# SELECT shows.title, tickets.price FROM shows
# JOIN tickets ON tickets.show_id = shows.id
#   AND tickets.price = (SELECT MAX(price) FROM tickets WHERE (tickets.show_id = shows.id))
```

### What raw joins will not do

The table argument has to be a table name. To join against a derived result
set, put the subquery in a CTE with `with_()` and join the CTE by its alias,
as in [Queries](./queries#common-table-expressions). Nested join conditions
beyond the `on`/`andOn`/`orOn` chain are not supported either; a condition
that complex is usually clearer as a CTE.

## Where to go next

| You want to | Read |
| --- | --- |
| Filter on a joined table | [Filtering](./filtering) |
| Load relations rather than join them | [Executing Queries](./executing#eager-loading-relations) |
| Turn a relation into a foreign key | [Schema and Migrations](./schema) |
| See every join method and mapping shape | [Model reference](./reference/model) |
