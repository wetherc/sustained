"""
Tests for AsyncAdapter.session(), which keeps a transaction's statements on
one database session.
"""

import sqlite3
import unittest

try:
    import duckdb

    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False

from sustained.aio import AsyncAdapter, DbApiAsyncAdapter, async_transaction
from sustained.aio_migrations import AsyncMigrator
from sustained.dialects import Dialects
from sustained.migrations import Migration


class CountingConnection:
    """A sqlite3 connection that counts and tracks the cursors it opens."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.cursors = []

    def cursor(self):
        cursor = self._conn.cursor()
        self.cursors.append(cursor)
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


def is_closed(cursor):
    try:
        cursor.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class TestDbApiSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = CountingConnection()
        self.adapter = DbApiAsyncAdapter(self.conn)

    async def test_statements_in_a_session_share_one_cursor(self):
        async with self.adapter.session():
            await self.adapter.execute("CREATE TABLE t (id INTEGER)", ())
            await self.adapter.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
            _, rows = await self.adapter.fetch("SELECT id FROM t", ())
            self.assertEqual(len(self.conn.cursors), 1)
            self.assertFalse(is_closed(self.conn.cursors[0]))
        self.assertEqual(rows, [(1,), (2,)])
        self.assertTrue(is_closed(self.conn.cursors[0]))

    async def test_a_nested_session_keeps_the_outer_cursor(self):
        async with self.adapter.session():
            async with self.adapter.session():
                await self.adapter.execute("SELECT 1", ())
            self.assertFalse(is_closed(self.conn.cursors[0]))
            await self.adapter.execute("SELECT 2", ())
        self.assertEqual(len(self.conn.cursors), 1)

    async def test_statements_outside_a_session_close_their_cursor(self):
        await self.adapter.execute("SELECT 1", ())
        await self.adapter.execute("SELECT 2", ())
        self.assertEqual(len(self.conn.cursors), 2)
        self.assertTrue(all(is_closed(c) for c in self.conn.cursors))

    async def test_a_transaction_runs_in_one_session(self):
        async with async_transaction(self.adapter):
            await self.adapter.execute("CREATE TABLE t (id INTEGER)", ())
            await self.adapter.execute("INSERT INTO t VALUES (1)", ())
        self.assertEqual(len(self.conn.cursors), 1)

    async def test_the_base_session_runs_its_block(self):
        ran = False
        async with AsyncAdapter().session():
            ran = True
        self.assertTrue(ran)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed")
class TestDuckdbAsyncTransaction(unittest.IsolatedAsyncioTestCase):
    """
    Every DuckDB cursor is its own session. BEGIN, the work and ROLLBACK
    must all reach the session cursor, or the work commits.
    """

    def setUp(self):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("CREATE TABLE t (id INTEGER)")
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def count(self):
        return self.conn.cursor().execute("SELECT count(*) FROM t").fetchone()[0]

    def tables(self):
        rows = (
            self.conn.cursor()
            .execute("SELECT table_name FROM information_schema.tables")
            .fetchall()
        )
        return {row[0] for row in rows}

    async def test_a_failed_block_rolls_its_writes_back(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            async with async_transaction(self.adapter, Dialects.DUCKDB):
                await self.adapter.execute("INSERT INTO t VALUES (1)", ())
                raise RuntimeError("boom")
        self.assertEqual(self.count(), 0)

    async def test_a_finished_block_commits_its_writes(self):
        async with async_transaction(self.adapter, Dialects.DUCKDB):
            await self.adapter.execute("INSERT INTO t VALUES (1)", ())
        self.assertEqual(self.count(), 1)

    async def test_a_failed_migration_leaves_no_schema_behind(self):
        bad = Migration(
            "001_bad",
            up=[
                "CREATE TABLE duck_things (id INTEGER)",
                "CREATE TABLE duck_things (id INTEGER)",
            ],
            down="DROP TABLE duck_things",
        )
        migrator = AsyncMigrator(self.adapter, [bad], dialect=Dialects.DUCKDB)
        with self.assertRaises(Exception):
            await migrator.up()
        self.assertNotIn("duck_things", self.tables())
        self.assertEqual([("001_bad", "pending")], await migrator.statuses())

    async def test_a_rehearsal_leaves_no_schema_behind(self):
        migration = Migration(
            "001_ducks",
            up="CREATE TABLE duck_rehearsal (id INTEGER)",
            down="DROP TABLE duck_rehearsal",
        )
        migrator = AsyncMigrator(self.adapter, [migration], dialect=Dialects.DUCKDB)
        results = await migrator.rehearse()
        self.assertEqual([(r.up_ok, r.down_ok) for r in results], [(True, True)])
        self.assertNotIn("duck_rehearsal", self.tables())
        self.assertEqual([], await migrator.applied_records())

    async def test_a_down_less_rehearsal_leaves_no_schema_behind(self):
        migration = Migration("001_ducks", up="CREATE TABLE duck_rehearsal (id INT)")
        migrator = AsyncMigrator(self.adapter, [migration], dialect=Dialects.DUCKDB)
        await migrator.rehearse()
        self.assertNotIn("duck_rehearsal", self.tables())


if __name__ == "__main__":
    unittest.main()
