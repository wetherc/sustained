"""
AsyncMigrator.status(), statuses() and validate() read the tracking table
without creating or upgrading it, so they run on a read-only replica.
"""

import os
import sqlite3
import tempfile
import unittest

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.migrations import Migration

MIGRATIONS = [Migration("a", up="CREATE TABLE ta (id INTEGER)")]


class TestAsyncReadOnlyStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        sqlite3.connect(self.path).close()
        self.conn = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
        )
        self.migrator = AsyncMigrator(DbApiAsyncAdapter(self.conn), MIGRATIONS)

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    async def test_status_reads_every_migration_pending(self):
        self.assertEqual(await self.migrator.status(), [("a", False)])

    async def test_statuses_reads_every_migration_pending(self):
        self.assertEqual(await self.migrator.statuses(), [("a", "pending")])

    async def test_validate_finds_no_problem(self):
        self.assertEqual(await self.migrator.validate(), [])

    async def test_nothing_is_written(self):
        await self.migrator.statuses()
        tables = self.conn.execute("SELECT name FROM sqlite_master").fetchall()
        self.assertEqual(tables, [])


if __name__ == "__main__":
    unittest.main()
