"""
Tests for the migration runner against in-memory SQLite.
"""

import sqlite3
import unittest

from sustained import Model
from sustained.migrations import Migration, Migrator, create_table_migration
from sustained.schema import Integer, String


class MigUser(Model):
    tableName = "mig_users"
    tableColumns = {
        "id": Integer(primary_key=True),
        "email": String(120, nullable=False),
    }


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {r[0] for r in rows}


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()


class TestMigrator(MigrationTestCase):
    def migrations(self):
        return [
            create_table_migration(MigUser),
            Migration(
                "add_flag",
                up="ALTER TABLE mig_users ADD COLUMN flag INTEGER DEFAULT 0",
                down=["ALTER TABLE mig_users DROP COLUMN flag"],
            ),
        ]

    def test_up_applies_in_order_and_records(self):
        migrator = Migrator(self.conn, self.migrations())
        applied = migrator.up()
        self.assertEqual(applied, ["create_mig_users", "add_flag"])
        self.assertIn("mig_users", table_names(self.conn))
        self.assertEqual(migrator.pending(), [])
        self.assertEqual(
            migrator.status(),
            [("create_mig_users", True), ("add_flag", True)],
        )

    def test_up_is_idempotent(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        self.assertEqual(migrator.up(), [])

    def test_up_to_target(self):
        migrator = Migrator(self.conn, self.migrations())
        applied = migrator.up(target="create_mig_users")
        self.assertEqual(applied, ["create_mig_users"])
        self.assertEqual(len(migrator.pending()), 1)

    def test_unknown_target_raises(self):
        migrator = Migrator(self.conn, self.migrations())
        with self.assertRaises(ValueError):
            migrator.up(target="nope")

    def test_down_reverts_newest_first(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        reverted = migrator.down()
        self.assertEqual(reverted, ["add_flag"])
        self.assertEqual(len(migrator.pending()), 1)
        reverted = migrator.down()
        self.assertEqual(reverted, ["create_mig_users"])
        self.assertNotIn("mig_users", table_names(self.conn))

    def test_down_requires_down_step(self):
        migrator = Migrator(
            self.conn, [Migration("one_way", up="CREATE TABLE ow (id INTEGER)")]
        )
        migrator.up()
        with self.assertRaises(ValueError):
            migrator.down()

    def test_down_requires_registered_migration(self):
        migrator = Migrator(self.conn, self.migrations())
        migrator.up()
        stripped = Migrator(self.conn, [])
        with self.assertRaises(ValueError):
            stripped.down()

    def test_failed_migration_rolls_back_tracking(self):
        migrations = [
            Migration(
                "ok", up="CREATE TABLE ok_tbl (id INTEGER)", down="DROP TABLE ok_tbl"
            ),
            Migration("boom", up="THIS IS NOT SQL"),
        ]
        migrator = Migrator(self.conn, migrations)
        with self.assertRaises(sqlite3.OperationalError):
            migrator.up()
        self.assertEqual(migrator.applied(), ["ok"])

    def test_callable_step(self):
        seen = []

        def make_it(conn):
            conn.execute("CREATE TABLE cb_tbl (id INTEGER)")
            seen.append(True)

        migrator = Migrator(self.conn, [Migration("cb", up=make_it)])
        migrator.up()
        self.assertTrue(seen)
        self.assertIn("cb_tbl", table_names(self.conn))

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            Migrator(
                self.conn,
                [Migration("a", up="SELECT 1"), Migration("a", up="SELECT 1")],
            )


if __name__ == "__main__":
    unittest.main()
