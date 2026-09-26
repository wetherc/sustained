"""
read_applied_records() reads a missing or earlier tracking table as no
history, and raises when the read fails for any other reason.
"""

import sqlite3
import unittest
from unittest import mock

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.migrations import Migration, Migrator
from sustained.migrations.core import bookkeeping


def migrations():
    return [Migration("001_t", up="CREATE TABLE t (id INTEGER)", down="DROP TABLE t")]


def refuse_tracking_reads(action, table, column, database, source):
    if action == sqlite3.SQLITE_READ and table == "sustained_migrations":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class TestReadAppliedRecords(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_a_missing_table_reads_as_no_history(self):
        self.assertEqual(Migrator(self.conn, migrations()).read_applied_records(), [])

    def test_an_earlier_table_reads_as_no_history(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        self.assertEqual(Migrator(self.conn, migrations()).read_applied_records(), [])

    def test_a_refused_read_raises(self):
        Migrator(self.conn, migrations()).up()
        self.conn.set_authorizer(refuse_tracking_reads)
        migrator = Migrator(self.conn, migrations())
        for read in (migrator.read_applied_records, migrator.status, migrator.validate):
            with self.subTest(read=read.__name__):
                with self.assertRaises(sqlite3.DatabaseError):
                    read()

    def test_a_closed_connection_raises(self):
        migrator = Migrator(self.conn, migrations())
        self.conn.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            migrator.read_applied_records()

    def test_a_failed_read_of_a_current_table_raises(self):
        Migrator(self.conn, migrations()).up()
        migrator = Migrator(self.conn, migrations())
        failure = sqlite3.OperationalError("disk I/O error")
        # Both migrators read the rows through the shared core.
        with mock.patch.object(bookkeeping, "read_records", side_effect=failure):
            with self.assertRaises(sqlite3.OperationalError):
                migrator.read_applied_records()


class TestAsyncReadAppliedRecords(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_a_missing_table_reads_as_no_history(self):
        migrator = AsyncMigrator(self.adapter, migrations())
        self.assertEqual(await migrator.read_applied_records(), [])

    async def test_an_earlier_table_reads_as_no_history(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        migrator = AsyncMigrator(self.adapter, migrations())
        self.assertEqual(await migrator.read_applied_records(), [])

    async def test_a_refused_read_raises(self):
        await AsyncMigrator(self.adapter, migrations()).up()
        self.conn.set_authorizer(refuse_tracking_reads)
        migrator = AsyncMigrator(self.adapter, migrations())
        with self.assertRaises(sqlite3.DatabaseError):
            await migrator.read_applied_records()


if __name__ == "__main__":
    unittest.main()
