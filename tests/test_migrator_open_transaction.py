"""
The migrator commits as it goes, so every call that writes refuses to run
inside a caller's open transaction block instead of committing the
caller's work with its own.
"""

import sqlite3
import unittest

from sustained.aio import DbApiAsyncAdapter, async_transaction
from sustained.aio_migrations import AsyncMigrator
from sustained.execution import transaction
from sustained.migrations import Callbacks, Migration, Migrator


def migrations():
    return [
        Migration("001_a", up="CREATE TABLE a (id INTEGER)", down="DROP TABLE a"),
        Migration("002_b", up="CREATE TABLE b (id INTEGER)", down="DROP TABLE b"),
    ]


def rows(conn):
    return conn.execute("SELECT id FROM caller").fetchall()


class RolledBack(Exception):
    """Raised to leave a transaction block through its rollback."""


class TestMigratorRefusesOpenTransaction(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE caller (id INTEGER)")
        self.conn.commit()
        self.migrator = Migrator(self.conn, migrations())
        self.migrator.up(target="001_a")

    def assert_refused(self, verb, call):
        with self.assertRaises(RolledBack):
            with transaction(self.conn):
                self.conn.execute("INSERT INTO caller VALUES (1)")
                with self.assertRaises(ValueError) as caught:
                    call()
                raise RolledBack
        self.assertEqual(
            str(caught.exception),
            f"{verb} cannot run inside an open transaction() block: it "
            "commits as it goes, and the commit would take the caller's "
            "work with it.",
        )
        # The caller's insert went back with its block.
        self.assertEqual(rows(self.conn), [])
        self.assertEqual(self.migrator.applied(), ["001_a"])

    def test_up(self):
        self.assert_refused("up", self.migrator.up)

    def test_up_refuses_before_the_callbacks(self):
        seen = []
        migrator = Migrator(
            self.conn,
            migrations(),
            callbacks=Callbacks(
                before_migrate=seen.append,
                on_error=lambda *args: seen.append(args),
            ),
        )
        self.assert_refused("up", migrator.up)
        self.assertEqual(seen, [])

    def test_down(self):
        self.assert_refused("down", self.migrator.down)

    def test_down_to(self):
        self.assert_refused("down_to", lambda: self.migrator.down_to("001_a"))

    def test_baseline(self):
        self.assert_refused("baseline", lambda: self.migrator.baseline("002_b"))

    def test_repair(self):
        self.assert_refused("repair", self.migrator.repair)

    def test_record_rehearsal(self):
        self.assert_refused(
            "record_rehearsal", lambda: self.migrator.record_rehearsal("k")
        )

    def test_runs_once_the_block_closes(self):
        with transaction(self.conn):
            self.conn.execute("INSERT INTO caller VALUES (1)")
        self.assertEqual(self.migrator.up(), ["002_b"])
        self.assertEqual(rows(self.conn), [(1,)])


class TestAsyncMigratorRefusesOpenTransaction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE caller (id INTEGER)")
        self.conn.commit()
        self.adapter = DbApiAsyncAdapter(self.conn)
        self.migrator = AsyncMigrator(self.adapter, migrations())
        await self.migrator.up(target="001_a")

    async def assert_refused(self, verb, call):
        with self.assertRaises(RolledBack):
            async with async_transaction(self.adapter):
                await self.adapter.execute("INSERT INTO caller VALUES (1)", ())
                with self.assertRaises(ValueError) as caught:
                    await call()
                raise RolledBack
        self.assertEqual(
            str(caught.exception),
            f"{verb} cannot run inside an open async_transaction() block: "
            "it commits as it goes, and the commit would take the caller's "
            "work with it.",
        )
        self.assertEqual(rows(self.conn), [])
        self.assertEqual(await self.migrator.applied(), ["001_a"])

    async def test_up(self):
        await self.assert_refused("up", self.migrator.up)

    async def test_up_refuses_before_the_callbacks(self):
        seen = []
        migrator = AsyncMigrator(
            self.adapter,
            migrations(),
            callbacks=Callbacks(
                before_migrate=seen.append,
                on_error=lambda *args: seen.append(args),
            ),
        )
        await self.assert_refused("up", migrator.up)
        self.assertEqual(seen, [])

    async def test_down(self):
        await self.assert_refused("down", self.migrator.down)

    async def test_down_to(self):
        await self.assert_refused("down_to", lambda: self.migrator.down_to("001_a"))

    async def test_baseline(self):
        await self.assert_refused("baseline", lambda: self.migrator.baseline("002_b"))

    async def test_repair(self):
        await self.assert_refused("repair", self.migrator.repair)

    async def test_record_rehearsal(self):
        await self.assert_refused(
            "record_rehearsal", lambda: self.migrator.record_rehearsal("k")
        )

    async def test_runs_once_the_block_closes(self):
        async with async_transaction(self.adapter):
            await self.adapter.execute("INSERT INTO caller VALUES (1)", ())
        self.assertEqual(await self.migrator.up(), ["002_b"])
        self.assertEqual(rows(self.conn), [(1,)])


if __name__ == "__main__":
    unittest.main()
