"""
A down step that fails where nothing rolls it back leaves partial
changes, so the migration's row is marked failed and validation blocks
the next run until the revert is finished and repair() clears the row.
"""

import sqlite3
import unittest
from unittest import mock

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.exceptions import MigrationError
from sustained.migrations import Callbacks, Migration, Migrator

FAILED = (
    "migration '002_b' has a failed attempt on record; clean up any "
    "partial changes, then run repair() and retry"
)


def migrations(transactional=False):
    return [
        Migration("001_a", up="CREATE TABLE a (id INTEGER)", down="DROP TABLE a"),
        Migration(
            "002_b",
            up=["CREATE TABLE b (id INTEGER)", "CREATE TABLE c (id INTEGER)"],
            down=["DROP TABLE c", "DROP TABLE missing"],
            transactional=transactional,
        ),
    ]


def tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sustained%'"
    ).fetchall()
    return {row[0] for row in rows}


class TestFailedDown(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.errors = []
        self.callbacks = Callbacks(
            on_error=lambda conn, migration_id, error: self.errors.append(
                (migration_id, type(error))
            )
        )

    def migrator(self, transactional=False):
        migrator = Migrator(
            self.conn, migrations(transactional), callbacks=self.callbacks
        )
        migrator.up()
        return migrator

    def test_marks_the_row_failed(self):
        migrator = self.migrator()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            migrator.down()
        self.assertEqual(caught.exception.migration_id, "002_b")
        self.assertEqual(self.errors, [("002_b", sqlite3.OperationalError)])
        # DROP TABLE c ran and stays dropped; nothing took it back.
        self.assertEqual(tables(self.conn), {"a", "b"})
        self.assertEqual(
            [(r.id, r.success) for r in migrator.read_applied_records()],
            [("001_a", True), ("002_b", False)],
        )
        self.assertEqual(migrator.validate(raise_on_problems=False), [FAILED])

    def test_refuses_the_next_down_until_repair(self):
        migrator = self.migrator()
        with self.assertRaises(sqlite3.OperationalError):
            migrator.down()
        with self.assertRaises(MigrationError) as caught:
            migrator.down()
        self.assertEqual(caught.exception.problems, [FAILED])
        # The refusal did not skip past 002_b to revert 001_a.
        self.assertIn("a", tables(self.conn))
        self.assertEqual(self.errors[-1], (None, MigrationError))
        self.conn.execute("DROP TABLE b")
        self.assertEqual(migrator.repair(), ["removed the failed attempt of '002_b'"])
        self.assertEqual(
            migrator.statuses(), [("001_a", "applied"), ("002_b", "pending")]
        )

    def test_baseline_records_a_restored_migration_after_repair(self):
        migrator = self.migrator()
        with self.assertRaises(sqlite3.OperationalError):
            migrator.down()
        self.conn.execute("CREATE TABLE c (id INTEGER)")
        migrator.repair()
        self.assertEqual(migrator.baseline("002_b"), ["002_b"])
        self.assertEqual(migrator.validate(raise_on_problems=False), [])
        self.assertEqual(migrator.applied(), ["001_a", "002_b"])

    def test_marks_the_row_on_an_engine_without_transactional_ddl(self):
        migrator = self.migrator(transactional=True)
        with mock.patch.object(
            migrator._compiler, "supports_transactional_ddl", return_value=False
        ):
            with self.assertRaises(sqlite3.OperationalError):
                migrator.down()
        self.assertEqual(migrator.validate(raise_on_problems=False), [FAILED])

    def test_a_rolled_back_down_leaves_the_row_applied(self):
        migrator = self.migrator(transactional=True)
        with self.assertRaises(sqlite3.OperationalError):
            migrator.down()
        self.assertEqual(tables(self.conn), {"a", "b", "c"})
        self.assertEqual(migrator.validate(raise_on_problems=False), [])
        self.assertEqual(migrator.applied(), ["001_a", "002_b"])

    def test_a_failed_mark_keeps_the_step_error(self):
        migrator = self.migrator()
        with mock.patch.object(migrator, "_run_sql", side_effect=RuntimeError("x")):
            with self.assertRaises(sqlite3.OperationalError):
                migrator.down()
        self.assertEqual(migrator.validate(raise_on_problems=False), [])

    def test_a_successful_down_fires_no_callback(self):
        migrator = Migrator(self.conn, migrations()[:1], callbacks=self.callbacks)
        migrator.up()
        self.assertEqual(migrator.down(), ["001_a"])
        self.assertEqual(self.errors, [])


class TestAsyncFailedDown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.errors = []
        self.callbacks = Callbacks(
            on_error=lambda adapter, migration_id, error: self.errors.append(
                (migration_id, type(error))
            )
        )

    async def migrator(self, transactional=False):
        migrator = AsyncMigrator(
            DbApiAsyncAdapter(self.conn),
            migrations(transactional),
            callbacks=self.callbacks,
        )
        await migrator.up()
        return migrator

    async def test_marks_the_row_failed(self):
        migrator = await self.migrator()
        with self.assertRaises(sqlite3.OperationalError) as caught:
            await migrator.down()
        self.assertEqual(caught.exception.migration_id, "002_b")
        self.assertEqual(self.errors, [("002_b", sqlite3.OperationalError)])
        self.assertEqual(await migrator.validate(raise_on_problems=False), [FAILED])
        with self.assertRaises(MigrationError) as refused:
            await migrator.down()
        self.assertEqual(refused.exception.problems, [FAILED])
        self.assertIn("a", tables(self.conn))

    async def test_a_rolled_back_down_leaves_the_row_applied(self):
        migrator = await self.migrator(transactional=True)
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.down()
        self.assertEqual(await migrator.validate(raise_on_problems=False), [])

    async def test_a_failed_mark_keeps_the_step_error(self):
        migrator = await self.migrator()
        with mock.patch.object(
            migrator._adapter, "commit", side_effect=RuntimeError("x")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await migrator.down()


if __name__ == "__main__":
    unittest.main()
