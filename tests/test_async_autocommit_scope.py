"""
An async migration with transactional=False runs with the driver's own
transaction control off, so SQLite honours the pragmas a rebuild ends with.
"""

import sqlite3
import unittest

from sustained.aio import AiosqliteAdapter, AsyncAdapter, DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.migrations import Migration

try:
    import aiosqlite
except ImportError:  # pragma: no cover - optional driver
    aiosqlite = None

# A data statement opens sqlite3's implicit transaction, and the pragma
# after it is ignored unless that transaction is off.
REBUILD = Migration(
    "rebuild",
    up=[
        "PRAGMA foreign_keys = OFF",
        "CREATE TABLE t (x INTEGER)",
        "INSERT INTO t VALUES (1)",
        "PRAGMA foreign_keys = ON",
    ],
    transactional=False,
)


class CommitCounter(AsyncAdapter):
    """An adapter in autocommit that counts its commits."""

    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


class TestDbApiAutocommitScope(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_the_closing_pragma_takes_effect(self):
        before = self.conn.isolation_level
        await AsyncMigrator(self.adapter, [REBUILD]).up()
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.conn.isolation_level, before)
        self.assertEqual(self.conn.execute("SELECT x FROM t").fetchall(), [(1,)])

    async def test_the_switch_is_restored_after_a_failure(self):
        before = self.conn.isolation_level
        failing = Migration("bad", up=["NOT SQL"], transactional=False)
        with self.assertRaises(sqlite3.OperationalError):
            await AsyncMigrator(self.adapter, [failing]).up()
        self.assertEqual(self.conn.isolation_level, before)


@unittest.skipIf(aiosqlite is None, "aiosqlite is not installed")
class TestAiosqliteAutocommitScope(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = await aiosqlite.connect(":memory:")
        await self.conn.execute("PRAGMA foreign_keys = ON")
        self.adapter = AiosqliteAdapter(self.conn)

    async def asyncTearDown(self):
        await self.conn.close()

    async def test_the_closing_pragma_takes_effect(self):
        await AsyncMigrator(self.adapter, [REBUILD]).up()
        cursor = await self.conn.execute("PRAGMA foreign_keys")
        self.assertEqual((await cursor.fetchone())[0], 1)
        level = await self.conn._execute(getattr, self.conn._conn, "isolation_level")
        self.assertEqual(level, "")


class TestBaseAutocommitScope(unittest.IsolatedAsyncioTestCase):
    async def test_the_base_commits_after_the_block(self):
        adapter = CommitCounter()
        async with adapter.autocommit_scope():
            self.assertEqual(adapter.commits, 0)
        self.assertEqual(adapter.commits, 1)


if __name__ == "__main__":
    unittest.main()
