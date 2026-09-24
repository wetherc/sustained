---
layout: default
title: Relations and Joins
---

Joins are defined once, in a model's `relationMappings`. Once a relation is declared, you join by its name instead of writing the `JOIN` condition each time:

```python
Show.query().select('shows.title', 'venues.name').innerJoinRelated('venue')
# SELECT shows.title, venues.name FROM shows
# INNER JOIN venues ON shows.venue_id = venues.id
```

Relations are also what `withGraphFetched()` loads and what the migrator turns into foreign keys. To join tables that lack a relation mapping, use a [raw join](#raw-joins).

The examples use the venue booking schema from [Getting Started](./getting-started).

## Declaring a relation

`relationMappings` is a dict from relation name to mapping. Each mapping needs three keys: the `relation` type, the `modelClass` it points at, and the `join` that connects the two tables.

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

The key is the name you pass to `innerJoinRelated('venue')` and `withGraphFetched('venue')`. It can be any name, and it does not have to match the joined table.

### Relation types

| Type | Cardinality | Eager loading attaches |
| --- | --- | --- |
| `BelongsToOneRelation` | many to one | one instance or `None` |
| `HasOneRelation` | one to one | one instance or `None` |
| `HasManyRelation` | one to many | a list |
| `ManyToManyRelation` | many to many, through a link table | a list |

The type does not change the SQL a `joinRelated` call produces, apart from the extra hop a `ManyToManyRelation` makes through its link table. It does change what eager loading attaches to each instance, and the migrator reads it to work out which side has the foreign key.

A `Show` belongs to one `Venue`, a `BelongsToOneRelation`, and the same relation seen from the other side is a `HasManyRelation`:

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

### Relations through a link table

A `ManyToManyRelation` adds a `through` key naming the link table and the two columns in it. An artist plays many shows, a show has many artists, and `show_artists` stores the pairs:

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

### Naming a model by string

`modelClass` takes the class itself or its name as a string. Every model with a `tableName` registers under its class name when the class body runs, so the string form resolves through that registry:

```python
'modelClass': Venue      # the class, when it is already imported
'modelClass': 'Venue'    # the name, resolved when the query is built
```

The string form prevents circular imports, because with it `Show` and `Venue` can point at each other from separate modules, as long as both classes exist by the time the query builds. A name that never resolves raises `ValueError`.

When two model classes share a name, a string reference to that name resolves to neither of them and raises `ValueError` naming both classes, unless the module that declares the relation defines the name itself. Pass the class when you have two models with one name.

## Joining a relation

Each major join type has its own method:

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

```python
Show.query().select('shows.title', 'venues.name').leftOuterJoinRelated('venue')
# SELECT shows.title, venues.name FROM shows
# LEFT OUTER JOIN venues ON shows.venue_id = venues.id
```

Once a query joins, use qualified column names. `select('name')` is ambiguous when both tables have a `name` column, and the database rejects it.

### Aliasing the joined table

Pass `alias` to name the joined table:

```python
Show.query().select('shows.title', 'v.name').innerJoinRelated('venue', alias='v')
# SELECT shows.title, v.name FROM shows
# INNER JOIN venues AS v ON shows.venue_id = v.id
```

### What a through join renders

Joining a `ManyToManyRelation` produces two joins. The join type you asked for applies to the far table. The hop to the link table is a `LEFT JOIN` for a left or full join, so an artist with no link row stays in the result, and an `INNER JOIN` for every other type:

```python
Artist.query().select('artists.name', 'shows.title').leftJoinRelated('shows')
# SELECT artists.name, shows.title FROM artists
# LEFT JOIN show_artists ON artists.id = show_artists.artist_id
# LEFT JOIN shows ON show_artists.show_id = shows.id
```

When you join through the same link table a second time, pass an `alias`. The second join renders the link table as `<alias>_<link table>`, so each copy of the link table has its own name. A repeated join through a link table without an alias raises `ValueError`:

```python
(
    Artist.query()
    .innerJoinRelated('shows', alias='booked')
    .leftJoinRelated('shows', alias='other')
)
# SELECT * FROM artists
# INNER JOIN show_artists ON artists.id = show_artists.artist_id
# INNER JOIN shows AS booked ON show_artists.show_id = booked.id
# LEFT JOIN show_artists AS other_show_artists ON artists.id = other_show_artists.artist_id
# LEFT JOIN shows AS other ON other_show_artists.show_id = other.id
```

## Loading relations instead of joining them

A join flattens the related rows into the same result rows, so a venue with five shows appears five times. `withGraphFetched()` runs a second query instead and attaches the results to each instance:

```python
venues = Venue.query().withGraphFetched('shows').run()

for venue in venues:
    print(venue.name, len(venue.shows))
```

A dotted path fetches nested relations, so `withGraphFetched('shows.tickets')` attaches each venue's shows and each show's tickets, at one query per level. See [Executing Queries](./executing#eager-loading-relations) for what it needs in the select list.

## Raw joins

For a join with no relation behind it, the raw methods take a table name directly. There is one per join type: `join`, `innerJoin`, `leftJoin`, `leftOuterJoin`, `rightJoin`, `rightOuterJoin`, `fullJoin`, `fullOuterJoin`, and `crossJoin`.

The simplest form is a table name and the three parts of an `ON` condition:

```python
Venue.query().leftJoin('shows', 'venues.id', '=', 'shows.venue_id')
# SELECT * FROM venues LEFT JOIN shows ON venues.id = shows.venue_id
```

`crossJoin` also takes the table on its own, since a cross join has no condition:

```python
Show.query().crossJoin('dates')
# SELECT * FROM shows CROSS JOIN dates
```

When the join columns share a name in both tables, `using` is shorter and produces one merged column instead of two:

```python
Show.query().join('show_artists', using=['show_id'])
# SELECT * FROM shows JOIN show_artists USING (show_id)
```

### Several conditions

Pass a callable in place of the three arguments and it receives a join builder with `on()`, `andOn()`, and `orOn()`. Each takes two columns and an operator:

```python
Show.query().join(
    'tickets',
    lambda j: (j
        .on('tickets.show_id', '=', 'shows.id')
        .andOn('tickets.price', '>', 'shows.floor_price')
    ),
)
# SELECT * FROM shows JOIN tickets
# ON tickets.show_id = shows.id AND tickets.price > shows.floor_price
```

Both sides of an `on()` are column references. To compare a column against a value, put the condition in `where()` instead, or wrap the value in `QueryBuilder.raw()`.

### A subquery on the right of ON

The right side of an `ON` condition can be a whole query, which renders parenthesized. This joins each show to its most expensive ticket:

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

The inner query renders like any other part of the statement. Under `to_sql()` its values become placeholders and join the outer parameter tuple, in the order they appear in the SQL.

### Joins against derived results

The table argument has to be a table name. To join against a derived result set, put the subquery in a CTE with `with_()` and join the CTE by its alias, as in [Queries](./queries#common-table-expressions). The join builder also does not support nested conditions beyond the `on`, `andOn`, and `orOn` chain, so write such a condition as a CTE.

## Where to go next

| You want to | Read |
| --- | --- |
| Filter on a joined table | [Filtering](./filtering) |
| Load relations rather than join them | [Executing Queries](./executing#eager-loading-relations) |
| Turn a relation into a foreign key | [Schema and Migrations](./schema) |
| See every join method and mapping key | [Model reference](./reference/model) |
