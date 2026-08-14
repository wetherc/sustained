"""
Tests for schema diffing, migration autogeneration, and rollback of
generated migrations, against in-memory SQLite.
"""

import sqlite3
import unittest

from sustained import Model, create_model
from sustained.autogenerate import (
    autogenerate,
    diff_schema,
    introspect_schema,
    normalize_type,
)
from sustained.dialects import Dialects
from sustained.migrations import Migration, Migrator
from sustained.schema import Boolean, Integer, String, Text


def make_model(name, table, columns):
    model = create_model(name, table)
    model.tableColumns = columns
    model.columns = tuple(columns)
    return model


class AutogenTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.User = make_model(
            f"AgU_{self.id().rsplit('.', 1)[-1]}",
            "ag_users",
            {
                "id": Integer(primary_key=True),
                "email": String(120, nullable=False),
            },
        )

    def tearDown(self):
        self.conn.close()


class TestNormalizeType(unittest.TestCase):
    def test_synonyms_and_parameters(self):
        self.assertEqual(normalize_type("VARCHAR(120)"), "VARCHAR")
        self.assertEqual(normalize_type("character varying"), "VARCHAR")
        self.assertEqual(normalize_type("NVARCHAR(MAX)"), "VARCHAR")
        self.assertEqual(normalize_type("DOUBLE PRECISION"), "FLOAT")
        self.assertEqual(normalize_type("jsonb"), "JSON")
        self.assertEqual(normalize_type("DATETIME2"), "TIMESTAMP")
        self.assertEqual(normalize_type("BIT"), "BOOLEAN")

    def test_unknown_passthrough(self):
        self.assertEqual(normalize_type("GEOMETRY"), "GEOMETRY")


class TestDiffSchema(AutogenTestCase):
    def test_missing_table_detected(self):
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.missing_tables, [self.User])
        self.assertIn("create table ag_users", diff.summary())

    def test_round_trip_is_empty(self):
        self.User.create_table(self.conn)
        diff = diff_schema(self.conn, [self.User])
        self.assertTrue(diff.is_empty())
        self.assertEqual(diff.summary(), "schema up to date")

    def test_new_column_detected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["bio"] = Text()
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(len(diff.new_columns), 1)
        self.assertEqual(diff.new_columns[0][1], "bio")

    def test_extra_objects_detected(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN legacy TEXT")
        self.conn.execute("CREATE TABLE orphan (id INTEGER)")
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.extra_columns, [("ag_users", "legacy")])
        self.assertEqual(diff.extra_tables, ["orphan"])

    def test_changed_column_detected(self):
        self.User.create_table(self.conn)
        changed = make_model(
            "AgChanged",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Boolean()},
        )
        diff = diff_schema(self.conn, [changed])
        self.assertEqual(len(diff.changed_columns), 1)
        self.assertIn("not auto-migrated", diff.summary())

    def test_tracking_table_excluded(self):
        self.conn.execute("CREATE TABLE sustained_migrations (id TEXT)")
        self.User.create_table(self.conn)
        diff = diff_schema(self.conn, [self.User])
        self.assertEqual(diff.extra_tables, [])

    def test_duplicate_table_declarations_rejected(self):
        other = make_model("AgDup", "ag_users", {"id": Integer(primary_key=True)})
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [self.User, other])

    def test_model_without_table_columns_rejected(self):
        bare = create_model("AgBare", "bare_tbl")
        with self.assertRaises(ValueError):
            diff_schema(self.conn, [bare])


class TestAutogenerate(AutogenTestCase):
    def test_up_to_date_returns_none(self):
        self.User.create_table(self.conn)
        self.assertIsNone(autogenerate(self.conn, [self.User], id="noop"))

    def test_create_table_migration_is_reversible(self):
        migration = autogenerate(self.conn, [self.User], id="m1")
        self.assertEqual(migration.down, ["DROP TABLE IF EXISTS ag_users"])
        self.assertIn("CREATE TABLE", migration.up[0])

    def test_add_column_migration_is_reversible(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["bio"] = Text()
        migration = autogenerate(self.conn, [self.User], id="m2")
        self.assertEqual(migration.up, ["ALTER TABLE ag_users ADD COLUMN bio TEXT"])
        self.assertEqual(migration.down, ["ALTER TABLE ag_users DROP COLUMN bio"])

    def test_drops_require_opt_in(self):
        self.User.create_table(self.conn)
        self.conn.execute("ALTER TABLE ag_users ADD COLUMN legacy TEXT")
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m3")
        migration = autogenerate(self.conn, [self.User], id="m3", allow_drops=True)
        self.assertEqual(migration.up, ["ALTER TABLE ag_users DROP COLUMN legacy"])
        self.assertIsNone(migration.down)

    def test_changed_columns_block_generation(self):
        self.User.create_table(self.conn)
        changed = make_model(
            "AgBlock",
            "ag_users",
            {"id": Integer(primary_key=True), "email": Boolean()},
        )
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [changed], id="m4")
        self.assertIsNone(
            autogenerate(self.conn, [changed], id="m4", ignore_changed_columns=True)
        )

    def test_not_null_add_without_default_rejected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["req"] = String(10, nullable=False)
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m5")

    def test_primary_key_add_rejected(self):
        self.User.create_table(self.conn)
        self.User.tableColumns["id2"] = Integer(primary_key=True)
        with self.assertRaises(ValueError):
            autogenerate(self.conn, [self.User], id="m6")


class TestMigratorSync(AutogenTestCase):
    def test_sync_creates_and_is_idempotent(self):
        migrator = Migrator(self.conn, [])
        applied = migrator.sync([self.User])
        self.assertEqual(len(applied), 1)
        self.assertTrue(diff_schema(self.conn, [self.User]).is_empty())
        self.assertEqual(migrator.sync([self.User]), [])

    def test_sync_then_down_rolls_back(self):
        migrator = Migrator(self.conn, [])
        migrator.sync([self.User])
        self.User.tableColumns["bio"] = Text()
        migrator.sync([self.User])
        migrator.down()
        columns = introspect_schema(self.conn)["ag_users"]
        self.assertNotIn("bio", columns)

    def test_down_to_target(self):
        migrator = Migrator(
            self.conn,
            [
                Migration("a", up="CREATE TABLE ta (id INTEGER)", down="DROP TABLE ta"),
                Migration("b", up="CREATE TABLE tb (id INTEGER)", down="DROP TABLE tb"),
                Migration("c", up="CREATE TABLE tc (id INTEGER)", down="DROP TABLE tc"),
            ],
        )
        migrator.up()
        reverted = migrator.down_to("a")
        self.assertEqual(reverted, ["c", "b"])
        self.assertEqual(migrator.applied(), ["a"])
        self.assertEqual(migrator.down_to("a"), [])

    def test_down_to_unapplied_target_raises(self):
        migrator = Migrator(self.conn, [])
        with self.assertRaises(ValueError):
            migrator.down_to("nope")


if __name__ == "__main__":
    unittest.main()
