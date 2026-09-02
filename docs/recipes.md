---
layout: default
title: Recipes
---

Each recipe here is an independent, standalone code snippet that demonstrates how to accomplish a common task using Sustained.

Every example uses the same four tables from [Getting Started](./getting-started), plus two more so the joins have somewhere to go.

```python
from sustained import Model, RelationType
from sustained.schema import Boolean, Integer, Numeric, String, Timestamp


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


class Artist(Model):
    tableName = 'artists'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'name': String(120, unique=True, nullable=False),
        'country': String(2),
    }
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


class Ticket(Model):
    tableName = 'tickets'
    tableColumns = {
        'id': Integer(primary_key=True, autoincrement=True),
        'show_id': Integer(references='shows.id', nullable=False),
        'price': Numeric(10, 2),
        'sold_at': Timestamp(),
    }
```

---

# Querying

## Reuse one filter for several queries

A builder changes in place, so a shared base query would collect every branch's filters. You can create a deep copy of it with `clone()`.

```python
sold_out = Show.query().where('sold_out', '=', True)

fillmore = sold_out.clone().where('venue_id', '=', 1).run()
first_ave = sold_out.clone().where('venue_id', '=', 2).run()
```

Without `clone()`, the second line would filter on both venues at once and return nothing.

## Filter with Python operators instead of strings

`Model.c` gives every column a typed reference. Use comparison operators to build the condition, and `&`, `|`, and `~` combine multiple conditions.

```python
from sustained import col

Show.query().where((Show.c.sold_out == True) & ~(Show.c.title.like('Cancelled%')))

# For a table with no model in scope:
Show.query().innerJoinRelated('venue').where(col('venues.capacity') > 1400)
```

Use `&` and `|`, never `and` and `or`. A `Predicate` raises `TypeError` in a boolean context, so the keyword version fails loudly instead of quietly evaluating to one side.

## Group a set of OR conditions

You can pass a lambda to any `where` method: it receives a builder whose conditions render inside parentheses for nested and grouped clauses.

```python
Show.query().where('venue_id', '=', 1).andWhere(
    lambda q: q.where('sold_out', '=', True).orWhere('starts_at', '>', '2026-09-01')
)
# WHERE venue_id = 1 AND (sold_out = TRUE OR starts_at > '2026-09-01')
```

Groups can nest arbitrarily deeply, the only limit is what your peers are willing to code review. The first condition in any chain must be a plain `where`, never `andWhere` or `orWhere`.

## Filter the result of an aggregate

`having()` takes the aggregate as written, because column aliases are not available to HAVING in standard SQL.

```python
revenue = (
    Ticket.query()
    .select('venues.name')
    .sum('tickets.price', alias='revenue')
    .count('tickets.id', alias='sold')
    .innerJoin('shows', 'tickets.show_id', '=', 'shows.id')
    .innerJoin('venues', 'shows.venue_id', '=', 'venues.id')
    .groupBy('venues.name')
    .having('SUM(tickets.price)', '>', 50)
    .orderBy('venues.name')
)

revenue.to_dicts()
# [{'name': 'First Avenue', 'revenue': 60, 'sold': 1},
#  {'name': 'The Fillmore', 'revenue': 120, 'sold': 3}]
```

## Rank rows inside a group

```python
ranked = (
    Ticket.query()
    .select('show_id', 'price')
    .select_window(
        'ROW_NUMBER', 'rank',
        partition_by=['show_id'],
        order_by=['price DESC'],
    )
)
# SELECT show_id, price,
#        ROW_NUMBER() OVER (PARTITION BY show_id ORDER BY price DESC) AS rank
# FROM tickets
```

To filter on `rank`, wrap this in a subquery. If you're using DuckDB, you can alternately use `qualify('rank <= 3')`: it is the only dialect that supports this syntax.

## Label rows with CASE

```python
Show.query().select('title').select_case(
    'status', 'open', when_clauses=[('sold_out = 1', 'sold out')]
)
# SELECT title, CASE WHEN sold_out = 1 THEN 'sold out' ELSE 'open' END AS status
```

Results are string literals by default. Wrap a result in `Column('other_col')` when it names a column instead.

## Feed one query into another with a CTE

```python
sold_out = Show.query().select('venue_id').where('sold_out', '=', True)

Venue.query() \
    .with_('sold_out_shows', sold_out) \
    .select('venues.name') \
    .innerJoin('sold_out_shows', 'venues.id', '=', 'sold_out_shows.venue_id')
```

A raw join needs a table name, so a derived result set has to become a CTE first and then join by its alias. Pass `recursive=True` to `with_()` for a self-referencing CTE.

## Filter by whether related rows exist

```python
from sustained import QueryBuilder

Venue.query().select('name').whereExists(
    Show.query()
    .select(QueryBuilder.raw('1'))
    .where('shows.venue_id', '=', QueryBuilder.raw('venues.id'))
    .andWhere('sold_out', '=', True)
)
```

Wrap the outer-query column in `QueryBuilder.raw()`. Without it, `'venues.id'` is read as a value to bind, not as a column to compare against.

## Page through a large table

`page()` uses `LIMIT` and `OFFSET`, which is fine for early pages and gets slower as the offset grows, because the database still walks the skipped rows.

```python
Show.query().page(2, 25)   # zero-based page number: rows 51-75
```

`cursor_page()` filters on the last value seen instead, so the cost stays flat however deep you go.

```python
first = Show.query().cursor_page('id', 100).run()
second = Show.query().cursor_page('id', 100, after=first[-1].id).run()
```

The cursor column must be unique and sorted, or you will have a bad time.

## Count the matches without running the query

`total()` runs `SELECT COUNT(*)` over the query with ordering, `LIMIT`, and `OFFSET` stripped, and leaves the original builder alone.

```python
query = Show.query().where('sold_out', '=', True)

count = query.total()   # 2
rows = query.limit(20).run()
```

## Get rows as dicts, a DataFrame, or Arrow table

```python
Show.query().to_dicts()   # [{'id': 1, 'title': 'Opening Night', ...}]
Show.query().to_df()      # pandas DataFrame
Show.query().to_arrow()   # pyarrow Table
```

`pandas` and `pyarrow` are optional dependencies; each method raises a `RuntimeError` naming the missing package if they can't be found.

---

# Relations

## Load a relation with the parent rows

```python
for venue in Venue.query().withGraphFetched('shows').run():
    print(venue.name, [s.title for s in venue.shows])
# The Fillmore ['Opening Night', 'Second Night']
# First Avenue ['Tour Finale']
```

Each relation costs one extra query. A `HasManyRelation` attaches a list; the to-one types attach a single instance or `None`. Keep the join key columns in your `select()`, or the second query will have nothing to match on.

## Join a many-to-many relation

Declare the link table in the `through` mapping, then join by relation name. Sustained joins the link table for you.

```python
Artist.query().select('artists.name', 'shows.title').innerJoinRelated('shows')
# SELECT artists.name, shows.title
# FROM artists
# INNER JOIN show_artists ON artists.id = show_artists.artist_id
# INNER JOIN shows ON show_artists.show_id = shows.id
```

The first hop is always an `INNER JOIN`. The join type you name applies to the second hop, which is where it changes the
result.

## Join the same table twice

Give the second join an alias and refer to it by that name.

```python
Show.query() \
    .select('shows.title', 'headline.name', 'support.name') \
    .innerJoinRelated('artists', alias='headline') \
    .leftJoinRelated('artists', alias='support')
```

## Reference a model defined in another file

Give `modelClass` the class name as a string. Every model with a `tableName` registers itself when its class is defined.

```python
'modelClass': 'Show'
```

The class must be imported before the query is built, or the name will not resolve and Sustained raises `ValueError`.

---

# Writing data

## Insert many rows at once

```python
Ticket.query().insert([
    {'show_id': 1, 'price': 45.0},
    {'show_id': 1, 'price': 45.0},
    {'show_id': 2, 'price': 30.0},
]).run()
```

Every row must have the same keys. Without a RETURNING clause this runs through the driver's `executemany()`, which is the fast path for bulk loads.

## Insert or update on conflict

```python
Artist.query().insert({'name': 'Low', 'country': 'GB'}) \
    .onConflict('name').merge().run()      # update the existing row

Artist.query().insert({'name': 'Duster', 'country': 'US'}) \
    .onConflict('name').ignore().run()     # keep the existing row
```

The conflict columns must have a unique constraint or primary key in the database, or the statement fails there. `merge()` updates every inserted column except the conflict columns; pass a list to narrow it. Postgres,
SQLite, and DuckDB render `ON CONFLICT`, MSSQL renders `MERGE`, and Presto raises `DialectError`.

## Get the generated id back from an insert

```python
rows = Venue.query().insert({'name': 'Roseland', 'city': 'Portland'}) \
    .returning('id').run()
# [{'id': 3}]
```

The statement returns dicts instead of a row count. MSSQL and Presto raise `DialectError`; use an `OUTPUT` clause through raw SQL on MSSQL.

## Copy rows between tables

```python
recent = Ticket.query().select('show_id', 'price').where('sold_at', '>', '2026-01-01')

TicketArchive.query().insert_from(['show_id', 'price'], recent).run()
```

For a new table instead of an existing one, `create_table_as('name')` turns a SELECT into CTAS. MSSQL raises; use `SELECT ... INTO` through raw SQL there.

## Update every row on purpose

`update()` and `delete()` raise `ValueError` without a `where()`. To override this guardrail, use a `WHERE` clause that always evaluates to `TRUE`:

```python
from sustained import QueryBuilder

Show.query().update({'sold_out': False}).where(QueryBuilder.raw('1'), '=', 1).run()
```

## Roll back a group of writes together

```python
with Model.transaction():
    Show.query().insert({'venue_id': 1, 'title': 'Added Date'}).run()
    Ticket.query().insert({'show_id': 4, 'price': 40.0}).run()
```

The block commits when it finishes and rolls back on any exception. Inside a transaction, `run()` stops committing per statement. Nested blocks use savepoints, so an inner failure rolls back only the inner block. DuckDB has no savepoints, so a nested block raises `DialectError` there.

---

# Schema changes

## Add a column

Add the new column to `tableColumns`, then let Sustained work out the difference.

```python
Show.tableColumns['support_act'] = String(200)

migrator.up(models=[Venue, Artist, Show, Ticket])
# ['auto_20260816133122_029439']
```

Pass every model you manage, not only the changed one. A table missing from the list will not be kept up to date.

## Preview the migration before it runs

```python
migration = migrator.plan([Venue, Artist, Show, Ticket])
if migration is not None:
    print(migration.up)
    # ['ALTER TABLE shows ADD COLUMN support_act VARCHAR(200)']
```

`plan()` returns `None` when the schema is current. It records nothing and applies nothing, so the migration it returns is not left pending. To validate your changes on the actual database before applying it, pass the same models to `rehearse(models=[...])`. Note that this is only supported for engines that allow rollbacks of schema changes; otherwise, apply your changes to a test database instead.

For the differences without the SQL, `diff_schema()` reports every gap:

```python
from sustained.autogenerate import diff_schema

print(diff_schema(conn, [Venue, Artist, Show, Ticket]).summary())
# add column shows.support_act
# drop column shows.legacy (destructive)
```

## Rename a column instead of dropping and re-adding it

A database catalog cannot tell a rename from a drop plus an add, so pass the explicit hints when needed:

```python
migrator.up(models=models, renames={'shows.name': 'title'})
migrator.up(models=models, table_renames={'gigs': 'shows'})
```

Without the hint, the old column is left alone and the new one is added beside it, because the catalog gives no reason to believe the rows should move.

## Drop a column

```python
migrator.up(models=models, allow_drops=True)
```

A migration containing a drop has no down step. The data cannot come back, so there is nothing to reverse.

## Add a NOT NULL column to a table with rows

Give the column a value for the rows that already exist, either a `default` or a `backfill`:

```python
'country': String(2, nullable=False, backfill='US')
```

Generation then emits three steps: add the new column as nullable, `UPDATE` the existing rows, then set `NOT NULL`. Without a value it refuses, because the existing rows would have nothing to hold.

## Write a migration by hand

Live your best life fam, we won't judge.

```python
from sustained.migrations import Migration, Migrator

migrations = [
    Migration(
        'split_artist_name',
        up=[
            'ALTER TABLE artists ADD COLUMN first_name TEXT',
            "UPDATE artists SET first_name = substr(name, 1, instr(name, ' ') - 1) WHERE 1 = 1",
        ],
        down='ALTER TABLE artists DROP COLUMN first_name',
    ),
]

Migrator(conn, migrations).up()
```

A step is a SQL string, a list of statements, or a callable that receives the connection.

## Keep a view in step with its definition

A view is replaced, not evolved, so it fits badly in a versioned migration. An `<id>.repeat.sql` file re-runs whenever its contents change.

```sql
-- upcoming_shows.repeat.sql
DROP VIEW IF EXISTS upcoming_shows;
CREATE VIEW upcoming_shows AS
  SELECT shows.title, venues.name AS venue
  FROM shows JOIN venues ON shows.venue_id = venues.id
  WHERE shows.sold_out = FALSE;
```

Keep the SQL safe to re-run: `CREATE OR REPLACE`, or drop the view first. Repeatables run after every versioned migration, have no down step, and `down` never touches them.

## Fill a value into a SQL migration at load time

```sql
-- 003_grant.up.sql
GRANT SELECT ON shows TO ${reader};
```

```python
from sustained.migration_files import load_migrations

migrations = load_migrations('migrations', placeholders={'reader': 'app_ro'})
```

Passing a mapping, even an empty one, turns substitution on: a `${key}` with no value then raises `ValueError` naming the file and the key. `$${` escapes a literal `${`. Substitution happens before the checksum is computed, so changing a value after a versioned migration ran shows up as a checksum mismatch.

## Adopt a database that already has the schema

```python
migrator.baseline('002_create_shows')   # record as applied, run nothing
migrator.up()                           # apply only what comes after
```

Rows carry checksums, so validation still catches later edits and a null execution time marks them as never having run.

---

# Deploys

## Wire up the command line

The `sustained` command reads a config module, `sustained_config` by default:

```python
# sustained_config.py
import psycopg

from models import Artist, Show, Ticket, Venue


def get_connection():
    return psycopg.connect('postgresql://localhost/app')


migrations_dir = 'migrations'
models = [Venue, Artist, Show, Ticket]
dialect = 'postgres'
```

Point at another module with `--config mymodule`. Naming `models` lets `plan` report model drift as well as pending migrations.

## Gate a deploy on what is waiting

`plan` exits `0` when the database is current, `2` when work is waiting, and `1` when validation found problems.

```bash
sustained plan
case $? in
  0) echo "nothing to do" ;;
  2) sustained rehearse && sustained migrate ;;
  *) exit 1 ;;
esac
```

`argparse` also exits `2` on a usage error, so a script that treats `2` as "work is waiting" should check stderr for an `error:` line.

## Read the plan in a pipeline

`status`, `validate`, and `plan` all accept a `--json` flag.

```console
$ sustained plan --json
{
  "pending": [],
  "problems": [],
  "drift": [
    "ALTER TABLE shows ADD COLUMN support_act VARCHAR(200)"
  ]
}
```

`drift` is `null`, not `[]`, when the config module names no models, so a caller can tell "nothing was compared" from "compared and found no gap". `statements` is `null` for a callable step, which has no SQL to count.

## Rehearse where the rollback cannot be trusted

Only SQLite, Postgres, and DuckDB roll schema changes back, so only they can rehearse against the real database. Elsewhere, point the rehearsal at a throwaway one:

```python
def get_rehearsal_connection():
    return psycopg.connect('postgresql://localhost/app_rehearsal')
```

The scratch database is usually empty, so the whole history replays instead of only what is pending, which proves the migrations run from nothing. The changes may survive the rollback there, so recreate the database before the
next rehearsal. In Python, this is `migrator.rehearse(scratch=True)`.

## Hand the SQL to a DBA instead of running it

Because not everything needs to be your problem.

```python
print(migrator.script('up'))     # every statement a run would execute
print(migrator.script('down'))   # the rollback
```

The output includes the tracking-table bookkeeping, so a run applied by hand still leaves the history correct.

## Run something before and after a migration

The config module can name three callbacks, and `migrate` calls whichever it finds:

```python
def before_migrate(connection):
    notify('migration starting')


def after_migrate(connection, applied):
    notify(f'applied {len(applied)} migrations')


def on_error(connection, migration_id, error):
    page_someone(f'{migration_id} failed: {error}')
```

`after_migrate` runs only when something applied, so a no-op run stays quiet. `migration_id` is `None` when the run failed before reaching a migration. Only `migrate` calls them; `rehearse` does not, because nothing real happened.

## Recover from a failed migration

A failed attempt is recorded, and validation then blocks the next run.

```console
$ sustained validate
problem  migration '004_trim' has a failed attempt on record; clean up any
         partial changes, then run repair() and retry
```

Clean up whatever the failed statements left behind, then clear the bookkeeping:

```console
$ sustained repair
repaired removed the failed attempt of '004_trim'
```

`repair` only fixes tracking rows. It does not undo half-applied schema changes, and it will not tell you which ones there were. You will need to manually correct any underlying problems and verify their fixes before `repair`-ing with Sustained.

## Accept an edit to a migration that already ran

Checksums cover the exact SQL text, so reformatting counts as an edit and validation reports a mismatch. When the edit was deliberate:

```console
$ sustained repair
repaired updated the stored checksum of 'create_venues'
```

Repeatables are the exception: their changed checksum is what schedules the re-run, so `repair` leaves it alone and the next `migrate` runs the new contents.

## Deploy from two machines at once

Nothing to configure. During a run the migrator holds an exclusive advisory lock named after the tracking table, so a second deploy waits instead of racing. Postgres uses `pg_advisory_lock`, MSSQL uses `sp_getapplock`, and MySQL uses `GET_LOCK`. The last two return a status rather than raising, so the run stops with `MigrationError` when the status says the lock is not held. SQLite and DuckDB serialize writers themselves.

Athena has no locking mechanism, so be careful to run only one migrator at a time there.

---

# Connections and drivers

## Match the driver to the dialect

The connection's parameter style must match the dialect's placeholder, or execution fails at the driver.

| Dialect | Driver | Placeholder |
| --- | --- | --- |
| `DEFAULT` | `sqlite3` | `?` |
| `POSTGRES` | `psycopg`, `psycopg2` | `%s` |
| `MSSQL` | `pyodbc` | `?` |
| `PRESTO` | `trino` | `?` |
| `ATHENA` | `pyathena` | `%s` |
| `MYSQL` | `PyMySQL`, `mysqlclient` | `%s` |
| `DUCKDB` | `duckdb` | `?` |

```python
from sustained.dialects import Dialects

Model.set_dialect(Dialects.POSTGRES)
Model.bind(psycopg.connect('dbname=app'))
```

Set the dialect on `Model` to cover every model, or on a subclass to scope it.

## Reuse connections across threads

```python
from sustained.pool import ConnectionPool

pool = ConnectionPool(lambda: psycopg.connect(DSN), max_size=10)
Model.bind(pool)
```

Bind the pool the way you would a connection. Each statement checks one out for its duration; a `transaction()` block pins one for the whole block. An exhausted pool raises `PoolTimeout` after its timeout.

## Run queries under asyncio

```python
from sustained.aio import DbApiAsyncAdapter

adapter = DbApiAsyncAdapter(sqlite3.connect('tour.db', check_same_thread=False))
Model.bind_async(adapter)

shows = await Show.query().where('sold_out', '=', True).arun()
```

`DbApiAsyncAdapter` runs a synchronous driver in a worker thread. `AiosqliteAdapter` and `AsyncpgAdapter` use their native drivers instead. The async path matches the sync one: eager loading covers dotted paths and `through` relations, and nested transactions use savepoints.

## Take the SQL and run it yourself

```python
sql, params = Show.query().where('sold_out', '=', True).to_sql()
# ('SELECT * FROM shows WHERE sold_out = ?', (True,))

cursor.execute(sql, params)
```

Sustained never opens a connection on its own, so this path works with any pool, proxy, or framework session you already have.

---

# Debugging

## See the SQL a query will run

```python
print(query)              # values inlined, for reading
print(query.to_sql())     # placeholders and parameters, as executed
```

`str(query)` is for logs and eyeballs. Never send its output to a database: the inlined values are formatted for reading, not for safety.

## Log every statement the application runs

```python
from sustained.execution import set_statement_listener

set_statement_listener(lambda sql, params, seconds: log.info('%s %r %.3fs', sql, params, seconds))
```

The listener fires after every executed statement. Pass `None` to remove it. It is global, not per model or connection.

## Read the query plan

```python
Show.query().where('sold_out', '=', True).explain()
Show.query().where('sold_out', '=', True).explain(analyze=True)
```

`analyze=True` executes the statement to measure it, so do not point it at a write. MSSQL raises `DialectError`; use `SET SHOWPLAN_XML` through raw SQL.

## Catch a column typo before the database does

Declare the model's columns. Any model with `tableColumns` gets this automatically.

```python
class Show(Model):
    tableName = 'shows'
    columns = ('id', 'venue_id', 'title', 'starts_at', 'sold_out')

Show.titel   # AttributeError, listing the declared columns
```

Without a declaration, every attribute resolves to a column name, and a typo reaches the database as a bad identifier.

## Find out which migration failed

Sustained tags the driver's exception with the id before re-raising it:

```python
try:
    migrator.up()
except Exception as error:
    print(getattr(error, 'migration_id', None))
```

From the shell the same information is on stderr: `error in '004_trim': column "legacy" does not exist`.
