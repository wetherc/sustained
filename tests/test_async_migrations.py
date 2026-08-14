"""
Async migration runner tests using the DbApiAsyncAdapter over SQLite.
"""

import sqlite3
import unittest

from sustained.aio import DbApiAsyncAdapter
from sustained.aio_migrations import AsyncMigrator
from sustained.migrations import Migration


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


class TestAsyncMigrator(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    def migrations(self):
        return [
            Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta"),
            Migration("b", up="CREATE TABLE tb (id INTEGER)", down="DROP TABLE tb"),
        ]

    async def test_up_applies_and_records(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        applied = await migrator.up()
        self.assertEqual(applied, ["a", "b"])
        self.assertIn("ta", table_names(self.conn))
        self.assertEqual(await migrator.pending(), [])
        self.assertEqual(await migrator.status(), [("a", True), ("b", True)])

    async def test_up_is_idempotent_and_targeted(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.up(target="a"), ["a"])
        self.assertEqual(await migrator.up(target="a"), [])
        self.assertEqual(await migrator.up(), ["b"])

    async def test_down_and_down_to(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        await migrator.up()
        self.assertEqual(await migrator.down(), ["b"])
        self.assertNotIn("tb", table_names(self.conn))
        await migrator.up()
        self.assertEqual(await migrator.down_to("a"), ["b"])
        self.assertEqual(await migrator.applied(), ["a"])

    async def test_failed_step_rolls_back(self):
        migrations = [
            Migration(
                "ok", up="CREATE TABLE ok_t (id INTEGER)", down="DROP TABLE ok_t"
            ),
            Migration("boom", up="THIS IS NOT SQL"),
        ]
        migrator = AsyncMigrator(self.adapter, migrations)
        with self.assertRaises(sqlite3.OperationalError):
            await migrator.up()
        self.assertEqual(await migrator.applied(), ["ok"])

    async def test_async_callable_step(self):
        seen = []

        async def make_it(adapter):
            await adapter.execute("CREATE TABLE cb_t (id INTEGER)", ())
            seen.append(True)

        migrator = AsyncMigrator(self.adapter, [Migration("cb", up=make_it)])
        await migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_t", table_names(self.conn))

    async def test_unknown_target_raises(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.up(target="nope")

    async def test_apply_records_checksum_and_seq(self):
        from sustained.migrations import migration_checksum

        migrations = self.migrations()
        migrator = AsyncMigrator(self.adapter, migrations)
        await migrator.up()
        rows = self.conn.execute(
            "SELECT id, seq, checksum, success FROM sustained_migrations "
            "ORDER BY seq"
        ).fetchall()
        self.assertEqual(
            rows,
            [
                ("a", 1, migration_checksum(migrations[0]), 1),
                ("b", 2, migration_checksum(migrations[1]), 1),
            ],
        )

    async def test_legacy_tracking_table_is_upgraded(self):
        self.conn.execute(
            "CREATE TABLE sustained_migrations "
            "(id VARCHAR(255) PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "INSERT INTO sustained_migrations VALUES ('a', '2024-01-01T00:00:00')"
        )
        self.conn.commit()
        migrator = AsyncMigrator(self.adapter, self.migrations())
        self.assertEqual(await migrator.applied(), ["a"])
        self.assertEqual(await migrator.up(), ["b"])
        rows = self.conn.execute(
            "SELECT id, seq, success FROM sustained_migrations ORDER BY seq"
        ).fetchall()
        self.assertEqual(rows, [("a", 1, 1), ("b", 2, 1)])

    async def test_validate_detects_edit_and_repair_accepts_it(self):
        from sustained.exceptions import MigrationError

        await AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x INTEGER)")]
        ).up()
        edited = AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x BIGINT)")]
        )
        with self.assertRaises(MigrationError):
            await edited.validate()
        actions = await edited.repair()
        self.assertEqual(actions, ["updated the stored checksum of 'a'"])
        self.assertEqual(await edited.validate(), [])

    async def test_up_validates_by_default(self):
        from sustained.exceptions import MigrationError

        await AsyncMigrator(
            self.adapter, [Migration("a", up="CREATE TABLE va (x INTEGER)")]
        ).up()
        edited = AsyncMigrator(
            self.adapter,
            [
                Migration("a", up="CREATE TABLE va (x BIGINT)"),
                Migration("b", up="CREATE TABLE vb (x INTEGER)"),
            ],
        )
        with self.assertRaises(MigrationError):
            await edited.up()
        self.assertEqual(await edited.up(validate=False), ["b"])

    async def test_baseline_records_without_running(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        recorded = await migrator.baseline("a")
        self.assertEqual(recorded, ["a"])
        self.assertNotIn("ta", table_names(self.conn))
        row = self.conn.execute(
            "SELECT id, seq, execution_ms, success FROM sustained_migrations"
        ).fetchone()
        self.assertEqual(row, ("a", 1, None, 1))
        self.assertEqual(await migrator.up(), ["b"])

    async def test_baseline_unknown_target_raises(self):
        migrator = AsyncMigrator(self.adapter, self.migrations())
        with self.assertRaises(ValueError):
            await migrator.baseline("nope")


if __name__ == "__main__":
    unittest.main()
