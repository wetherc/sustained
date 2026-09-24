"""
baseline() refuses an id that has a failed attempt on record, because a
second row for that id breaks the tracking table's primary key, and it
rolls back every row it inserted when a write fails part way.
"""

import sqlite3
import unittest
from unittest import mock

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.exceptions import MigrationError
from sustained.migrations import Migration, Migrator

FAILED = (
    "migration '002_b' has a failed attempt on record; clean up any "
    "partial changes, then run repair() and retry"
)


def migrations():
    return [
        Migration("001_a", up="CREATE TABLE a (id INTEGER)"),
        Migration(
            "002_b",
            up=["CREATE TABLE b (id INTEGER)", "CREATE TABLE b (id INTEGER)"],
            transactional=False,
        ),
        Migration("003_c", up="CREATE TABLE c (id INTEGER)"),
    ]


def rows(conn):
    return conn.execute(
        "SELECT id, success FROM sustained_migrations ORDER BY seq"
    ).fetchall()


def failing_on_second_insert(original, insert_sql):
    calls = []

    def run(sql, params=None):
        if sql == insert_sql:
            calls.append(sql)
            if len(calls) == 2:
                raise RuntimeError("write refused")
        return original(sql, params)

    return run


class TestBaselineFailedRow(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.migrator = Migrator(self.conn, migrations())

    def fail_002(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.migrator.up()
        self.assertEqual(rows(self.conn), [("001_a", 1), ("002_b", 0)])

    def test_refuses_an_id_with_a_failed_row(self):
        self.fail_002()
        with self.assertRaises(MigrationError) as caught:
            self.migrator.baseline("003_c")
        self.assertEqual(caught.exception.problems, [FAILED])
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(rows(self.conn), [("001_a", 1), ("002_b", 0)])

    def test_a_failed_row_past_the_target_does_not_block(self):
        self.fail_002()
        self.assertEqual(self.migrator.baseline("001_a"), [])

    def test_records_after_repair(self):
        self.fail_002()
        self.migrator.repair()
        self.assertEqual(self.migrator.baseline("003_c"), ["002_b", "003_c"])

    def test_a_failed_write_rolls_back_earlier_rows(self):
        run = failing_on_second_insert(
            self.migrator._run_sql, self.migrator._insert_sql()
        )
        with mock.patch.object(self.migrator, "_run_sql", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "write refused"):
                self.migrator.baseline("003_c")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(rows(self.conn), [])


class TestAsyncBaselineFailedRow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.migrator = AsyncMigrator(DbApiAsyncAdapter(self.conn), migrations())

    async def test_refuses_an_id_with_a_failed_row(self):
        with self.assertRaises(sqlite3.OperationalError):
            await self.migrator.up()
        with self.assertRaises(MigrationError) as caught:
            await self.migrator.baseline("003_c")
        self.assertEqual(caught.exception.problems, [FAILED])
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(rows(self.conn), [("001_a", 1), ("002_b", 0)])

    async def test_a_failed_write_rolls_back_earlier_rows(self):
        original = self.migrator._execute
        insert_sql = self.migrator._insert_sql()
        calls = []

        async def execute(sql, params=None):
            if sql == insert_sql:
                calls.append(sql)
                if len(calls) == 2:
                    raise RuntimeError("write refused")
            return await original(sql, params)

        with mock.patch.object(self.migrator, "_execute", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "write refused"):
                await self.migrator.baseline("003_c")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(rows(self.conn), [])


if __name__ == "__main__":
    unittest.main()
