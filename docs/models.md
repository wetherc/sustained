---
layout: default
title: Defining Models
---

A model is a Python class with a table name on it:

```python
from sustained import Model

class Venue(Model):
    tableName = 'venues'
```

That is enough to build queries. Everything else on this page is optional,
and each addition buys one specific thing: qualified names, typo checking,
typed columns, a dialect, a connection, or a schema the migrator can manage.

The examples use the venue booking schema from
[Getting Started](./getting-started): venues hold shows, shows sell tickets,
and artists play shows through a link table.

## Naming the table

`tableName` is the only required attribute. Add `tableSchema` and `database`
when the table needs a qualified name:

```python
class Venue(Model):
    database = 'analytics'
    tableSchema = 'public'
    tableName = 'venues'

print(Venue.query())
# SELECT * FROM analytics.public.venues
```

The parts join with dots in the order database, schema, table. Both extra
attributes default to `None`, and a `None` part is left out. Quoting follows
the dialect, so the same class renders `"analytics"."public"."venues"` on
Postgres.

## Columns as attributes

Any attribute on a model class resolves to the qualified column name:

```python
Venue.city
# 'venues.city'

Venue.query().select(Venue.name, Venue.city)
# SELECT venues.name, venues.city FROM venues
```

Qualified names matter in joins, where two tables can both have a `name`
column. They come from the same three parts as the table name, so a model
with a `database` and `tableSchema` produces `analytics.public.venues.city`.

Instances behave the same way. `Venue().city` is also `'venues.city'`, which
means a hydrated instance returns the column name for any attribute the query
did not populate.

## Catching column typos

Attribute access is generous by default: an undeclared name still resolves,
so `Venue.citty` becomes the string `'venues.citty'` and the mistake surfaces
as a database error at run time. Declare the column names to close that gap:

```python
class Venue(Model):
    tableName = 'venues'
    columns = ('id', 'name', 'city', 'capacity')

Venue.citty
# AttributeError: 'Venue' does not declare a column named 'citty'.
# Declared columns: id, name, city, capacity.
```

The check runs on the class, on instances, and on the `Model.c` namespace
below. It does not run on the string arguments to `select()` or `where()`,
which are passed through to the SQL as written.

Declaring `tableColumns` sets `columns` for you from the same keys, so a
model with a typed schema gets the check without repeating the names:

```python
from sustained.schema import Integer, String

class Venue(Model):
    tableName = 'venues'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'name': String(120, nullable=False),
        'city': String(80, nullable=False),
        'capacity': Integer(),
    }

Venue.columns
# ('id', 'name', 'city', 'capacity')
```

Set `columns` explicitly alongside `tableColumns` to override that, which is
what you want when the table has columns the migrator should not manage.

`tableColumns` is also what the migrator diffs against the live database. See
[Schema and Migrations](./schema) for the column types and what a change to
one generates.

## Typed columns

`Model.c` gives every column a reference that builds conditions from Python
operators instead of operator strings:

```python
Venue.c.capacity
# ColumnExpr('venues.capacity')

Venue.query().where((Venue.c.capacity > 1000) & (Venue.c.city == 'Minneapolis'))
# SELECT * FROM venues WHERE (venues.capacity > 1000 AND venues.city = 'Minneapolis')
```

The result of a comparison is a `Predicate`, which combines with `&`, `|`,
and `~`. Pass it to `where()` or `having()`. For a table with no model in
scope, `col('show_artists.artist_id')` builds the same kind of reference from
a dotted path.

[Filtering](./filtering#typed-predicates) covers the full operator set and the
methods for LIKE, IN, BETWEEN, and NULL.

## Finding models by name

Every subclass with a `tableName` registers itself under its class name when
the class body executes. Relation mappings use that registry, so a mapping
can name its target as a string:

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

The string form is what breaks import cycles: `Venue` and `Show` can point at
each other from separate modules as long as both classes exist before the
query is built. A name that never resolves raises `ValueError` at build time,
naming the reference it could not find.

`get_registered_model('Show')` returns the class or `None`.
`resolve_model_reference` takes a class or a name and returns the class,
raising for an unresolvable name.

Two model classes with the same name in different modules overwrite each
other in the registry, and the last one defined wins. Pass the class itself
rather than a name when that is a risk.

## The dialect and the connection

Both are class attributes, set once and inherited by subclasses:

```python
from sustained.dialects import Dialects

Venue.set_dialect(Dialects.POSTGRES)   # quoting, placeholders, function names
Venue.bind(psycopg.connect(DSN))       # every query on Venue can now run()
```

Setting either on `Model` itself applies to every model that does not set its
own. Setting it on a subclass scopes it to that subclass. `Model.unbind()`
removes a binding, and passing a connection to `run()` overrides one.

Two models with different dialects can coexist, which is how a query against
Postgres and a query against Athena run in one process.
[SQL Dialects](./dialects) pairs each dialect with its driver and lists what
it cannot do. [Executing Queries](./executing) covers binding, pooling, and
async adapters.

## Models built at run time

`create_model()` returns a model class from a name and a table name, for
schemas discovered at run time rather than written down:

```python
from sustained import create_model

Venue = create_model('Venue', 'venues', columns=('id', 'name', 'city'))

Venue.query().select('id', 'name')
# SELECT id, name FROM venues
```

It takes the same optional pieces as a class body: `mappings` for
`relationMappings`, `columns` for the strict column set, and `table_schema`
and `database` for the qualified name. The result registers itself under the name
you pass, so string references in other models resolve to it.

```python
Show = create_model(
    'Show',
    'shows',
    mappings={
        'venue': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': Venue,
            'join': {'from': 'shows.venue_id', 'to': 'venues.id'},
        },
    },
)

Show.query().innerJoinRelated('venue')
# SELECT * FROM shows INNER JOIN venues ON shows.venue_id = venues.id
```

## Where to go next

| You want to | Read |
| --- | --- |
| Build a SELECT from a model | [Queries](./queries) |
| Filter rows | [Filtering](./filtering) |
| Join two models together | [Relations and Joins](./relations) |
| Run the query and get instances back | [Executing Queries](./executing) |
| Have Sustained create and migrate the table | [Schema and Migrations](./schema) |
| Look up an attribute or method exactly | [Model reference](./reference/model) |
