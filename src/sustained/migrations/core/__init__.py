"""
The logic Migrator and AsyncMigrator share, written once.

Every run is a generator here. It never touches a connection: where it
needs the database, a callback, or a block such as a transaction, it
yields a request from `requests` and carries on with the answer. Migrator
answers the requests on a blocking connection and AsyncMigrator on an
async adapter, each in a short loop of its own, so the two migrators run
the same statements in the same order and differ only in how a statement
reaches the driver.

The modules split the work by concern: the requests and their helpers
(`requests`), the state a migrator holds (`base`), the tracking table,
the rehearsal rows and the lock (`bookkeeping`), up() and down()
(`runs`), and rehearse() (`rehearsing`).
"""
