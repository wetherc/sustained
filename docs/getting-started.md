---
layout: default
title: Getting Started
---

Sustained needs no database to start, and can be used purely to generate SQL based on data models that you define. With no other setup, you can just describe a table, and start printing the SQL against it:

```python
from sustained import Model

class Show(Model):
    tableName = 'shows'

print(Show.query().select('title').where('sold_out', '=', False))
# SELECT title FROM shows WHERE sold_out = FALSE
```

This guide will take that one class and grow it into a full, working application. This will include a schema Sustained creates for you, queries that join across tables, schema changes added through a generated, scripted migration, and the same migrations run from an interactive command line. Everything here runs against SQLite through the Python standard library, so there is nothing to install but Sustained itself.

Each step in this guide builds on the ones before it, and we recommend running through it top-to-bottom in one sitting if you're new to the package.

## Install

```bash
python3 -m pip install sustained
```

Sustained has no required dependencies. `pandas` and `pyarrow` are optional, and only the `to_df()` and `to_arrow()` methods need them.

## Describe your tables

A model is a class with a table name. You can optionally also define its `tableColumns`; doing so allows Sustained to manage its schema changes.

Save this as `venues.py`. It is the schema for the rest of the guide: venues that host shows.

```python
from sustained import Model, RelationType
from sustained.schema import Boolean, Integer, String, Timestamp


class Venue(Model):
    tableName = 'venues'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'name': String(120, nullable=False),
        'city': String(80, nullable=False),
        'capacity': Integer(),
    }
    relationMappings = {
        'shows': {
            'relation': RelationType.HasManyRelation,
            'modelClass': 'Show',
            'join': {'from': 'venues.id', 'to': 'shows.venue_id'},
        },
    }


class Show(Model):
    tableName = 'shows'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'venue_id': Integer(references='venues.id', nullable=False),
        'title': String(200, nullable=False),
        'starts_at': Timestamp(),
        'sold_out': Boolean(default=False),
    }
    relationMappings = {
        'venue': {
            'relation': RelationType.BelongsToOneRelation,
            'modelClass': 'Venue',
            'join': {'from': 'shows.venue_id', 'to': 'venues.id'},
        },
    }
```

Two things to notice. `modelClass` names `'Show'` as a string, because `Show` is not defined yet when `Venue` is read; the name resolves through the modelregistry when a query needs it. And declaring `tableColumns` turns on strict column names: `Show.c.titel` raises `AttributeError` instead of reaching the database as a bad column.

## Build a query before you connect

A query builder does not need a connection. You can print it to see the SQL, or call `to_sql()` for the parameterized form a driver expects.

```python
from venues import Show

query = Show.query().select('title').where('sold_out', '=', False)

print(query)
# SELECT title FROM shows WHERE sold_out = FALSE

print(query.to_sql())
# ('SELECT title FROM shows WHERE sold_out = ?', (False,))
```

`str(query)` inlines values as literals, for reading and logging. `to_sql()` returns the SQL with placeholders and the values as a separate tuple. Every statement Sustained executes takes the second form, so user-supplied values never travel as text inside the SQL itself.

## Create the schema

`Migrator.up(models=[...])` compares the models you've defined in your code against the live database, generates any migration needed to catch the database up, records it, and applies it. Note that the source of truth is always assumed to be the table you've defined in code, and Sustained will always defer to that rather than the live database.

```python
import sqlite3

from sustained import Model
from sustained.migrations import Migrator
from venues import Show, Venue

conn = sqlite3.connect('tour.db')
Model.bind(conn)

migrator = Migrator(conn, [])
migrator.up(models=[Venue, Show])
# ['auto_20260816133122_029439']
```

`Model.bind()` on the base class shares one connection with every model. Bind a subclass instead to scope a connection to it.

When running a migration, pass every model you manage to the migrator, not only the ones that changed. The diff compares the whole database against the whole list, and a table missing from the list could silently miss updates.

## Write and read rows

```python
Venue.query().insert([
    {'name': 'The Fillmore', 'city': 'San Francisco', 'capacity': 1315},
    {'name': 'First Avenue', 'city': 'Minneapolis', 'capacity': 1550},
]).run()

venues = {v.name: v.id for v in Venue.query().run()}

Show.query().insert([
    {'venue_id': venues['The Fillmore'], 'title': 'Opening Night', 'sold_out': True},
    {'venue_id': venues['The Fillmore'], 'title': 'Second Night', 'sold_out': False},
    {'venue_id': venues['First Avenue'], 'title': 'Tour Finale', 'sold_out': True},
]).run()

for show in Show.query().where('sold_out', '=', True).orderBy('title').run():
    print(show.title)
# Opening Night
# Tour Finale
```

`run()` hydrates each row into a model instance. `first()` runs the same query with `LIMIT 1` and returns one instance or `None`. Writes commit and return the number of rows they touched.

`update()` and `delete()` statements both will refuse to run if no filter is supplied, and will raise `ValueError` when the query has no `where()`. Any destructive action in Sustained will always require explicit user permission to run; if you want to mass-update or mass-delete all rows in a table, supply a where clause that always evaluates to `TRUE`.

## Join across a relation

`innerJoinRelated()` reads the join condition from `relationMappings`, so joins can be made without re-declaring the key columns every time you go to make a join.

```python
from sustained import col

query = (
    Show.query()
    .select('shows.title', 'venues.city')
    .innerJoinRelated('venue')
    .where(col('venues.capacity') > 1400)
)

print(query)
# SELECT shows.title, venues.city
# FROM shows
# INNER JOIN venues ON shows.venue_id = venues.id
# WHERE venues.capacity > 1400

print(query.to_dicts())
# [{'title': 'Tour Finale', 'city': 'Minneapolis'}]
```

`col('venues.capacity') > 1400` is a typed predicate. Python's comparison operators build the condition, and `&`, `|`, and `~` boolean operators combine multiple conditions. The three-argument form, `where('capacity', '>', 1400)`, does the same thing.

Grouping and aggregates work the way the SQL does:

```python
query = (
    Show.query()
    .select('venues.city')
    .count('shows.id', alias='shows')
    .innerJoinRelated('venue')
    .groupBy('venues.city')
    .orderBy('venues.city')
)

print(query.to_dicts())
# [{'city': 'Minneapolis', 'shows': 1}, {'city': 'San Francisco', 'shows': 2}]
```

To load a relation instead of joining it, use `withGraphFetched()`. It runs one extra query per relation and attaches the results to each instance:

```python
for venue in Venue.query().withGraphFetched('shows').run():
    print(venue.name, [s.title for s in venue.shows])
# The Fillmore ['Opening Night', 'Second Night']
# First Avenue ['Tour Finale']
```

## Change a model and migrate it

Add a column to the model, then ask what a migration would do about it: `plan()` returns the migration that `up(models=[...])` would generate, without recording or running anything. This allows you to safely preview the actions your migration would take against the database.

```python
from sustained.schema import String

Show.tableColumns['support_act'] = String(200)

migration = migrator.plan([Venue, Show])
print(migration.up)
# ['ALTER TABLE shows ADD COLUMN support_act VARCHAR(200)']

migrator.up(models=[Venue, Show])   # generate, record, apply
migrator.down()                     # revert the newest applied migration
```

Migration generation refuses to guess about anything that loses data. Dropping a table or column needs `allow_drops=True`. A rename needs a hint, because the database catalog cannot tell a rename operation apart from a drop plus an add. Tightening a column to NOT NULL needs a `default` or a `backfill` value for the rows that already exist. Read [Schema and Migrations](./schema) for each rule.

## Move migrations to the shell

Generated migrations are suitable for development in a notebook. For deploys, you can keep migrations as files you can commit and review, and run them with the `sustained` CLI command.

A migration is a pair of SQL files named for its id. Migrations' order of execution is determined by their ID, sorted in ascending order.

```
migrations/
  001_create_venues.up.sql
  001_create_venues.down.sql
  002_create_shows.up.sql
  002_create_shows.down.sql
```

```sql
-- 001_create_venues.up.sql
CREATE TABLE venues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name VARCHAR(120) NOT NULL,
  city VARCHAR(80) NOT NULL,
  capacity INTEGER
);
```

```sql
-- 001_create_venues.down.sql
DROP TABLE venues;
```

A config module tells the command where the database and the migrations are. Save it as `sustained_config.py` next to the migrations directory:

```python
import sqlite3

from venues import Show, Venue


def get_connection():
    return sqlite3.connect('tour.db')


migrations_dir = 'migrations'
models = [Venue, Show]
```

`models` is optional. Including it lets `sustained plan` also report the gap between your models and the database.

Now read the plan. It returns with exit code 2 when work is waiting, so a deploy script can branch on it:

```console
$ sustained plan
pending
  001_create_venues  1 statement
  002_create_shows   1 statement

2 pending migrations
run: sustained migrate
```

Rehearse the changes before you apply them. This runs every pending migration, runs the downgrade steps back down, and rolls the whole thing back, so you learn whether the SQL is valid and whether it reverses while the real schema is still untouched:

```console
$ sustained rehearse
rehearsed 001_create_venues  up ok, down ok
rehearsed 002_create_shows   up ok, down ok
rollback complete, database unchanged
```

Then apply it:

```console
$ sustained migrate
applied  001_create_venues
applied  002_create_shows

$ sustained status
applied  001_create_venues
applied  002_create_shows
```

`sustained down --steps 1` reverts the newest migration. `--steps` must be 1 or more. `sustained down --to 001_create_venues` reverts until that migration is the newest applied.

A rehearsal proves the statements are valid and that the down steps reverse them. It does not indicate anything about how long they take to execute on a production-sized table.

Only databases whose schema changes roll back can rehearse: SQLite, Postgres, and DuckDB. Elsewhere, point the rehearsal at a scratch database with `get_rehearsal_connection()` in the config module. This is based purely on each engine's level of support for rolling back schema changes. More detail on this is available in [Schema and Migrations](./schema).

## Where to go next

You have the whole loop: models, queries, generated migrations, and CLI-driven migrations.

| To learn | Read |
| --- | --- |
| A task you already have in mind | [Recipes](./recipes) |
| What a method takes and returns | [API Reference](./reference/) |
| The migration rules in full | [Schema and Migrations](./schema) |
| Supported SQL engines | [SQL Dialects](./dialects) |
| Filters, groups, joins, and relations | [Filtering](./filtering), [Grouping](./grouping), [Relations and Joins](./relations) |
| Transactions, pooling, and async | [Executing Queries](./executing) |
