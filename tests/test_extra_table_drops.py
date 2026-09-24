"""
Dropping tables no model declares. The engine refuses to drop a table
that another table's foreign key still names, so a child goes before
its parent, and tables in a cycle lose their keys first.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate
from sustained.dialects import Dialects
from sustained.introspect import (
    IntrospectedColumn,
    IntrospectedForeignKey,
    IntrospectedTable,
    Snapshot,
)
from sustained.schema import Integer


def kept():
    model = create_model("KeptTable", "kept")
    model.tableColumns = {"id": Integer(primary_key=True)}
    model.columns = ("id",)
    return model


class TestSqliteDropOrder(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("CREATE TABLE kept (id INTEGER PRIMARY KEY)")

    def run_migration(self):
        migration = autogenerate(self.conn, [kept()], id="m", allow_drops=True)
        for statement in migration.up:
            self.conn.execute(statement)
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(tables, {"kept"})
        return migration

    def test_a_child_drops_before_its_parent(self):
        # sqlite_master lists the parent first, since it was created first.
        self.conn.execute("CREATE TABLE a_parent (id INTEGER PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE b_child (id INTEGER PRIMARY KEY, "
            "parent_id INTEGER REFERENCES a_parent (id))"
        )
        self.conn.execute("INSERT INTO a_parent VALUES (1)")
        self.conn.execute("INSERT INTO b_child VALUES (1, 1)")
        migration = self.run_migration()
        self.assertEqual(
            migration.up, ['DROP TABLE "b_child"', 'DROP TABLE "a_parent"']
        )
        self.assertTrue(migration.transactional)

    def test_a_cycle_drops_with_enforcement_off(self):
        self.conn.execute(
            "CREATE TABLE a_one (id INTEGER PRIMARY KEY, "
            "two_id INTEGER REFERENCES b_two (id))"
        )
        self.conn.execute(
            "CREATE TABLE b_two (id INTEGER PRIMARY KEY, "
            "one_id INTEGER REFERENCES a_one (id))"
        )
        self.conn.execute("INSERT INTO a_one VALUES (1, NULL)")
        self.conn.execute("INSERT INTO b_two VALUES (1, 1)")
        self.conn.execute("UPDATE a_one SET two_id = 1")
        migration = self.run_migration()
        self.assertEqual(migration.up[0], "PRAGMA foreign_keys = OFF")
        self.assertEqual(migration.up[-1], "PRAGMA foreign_keys = ON")
        self.assertFalse(migration.transactional)


def table(*keys):
    return IntrospectedTable(
        columns={"id": IntrospectedColumn("integer", False, True)},
        primary_key=("id",),
        foreign_keys={
            name: IntrospectedForeignKey(("id",), target, ("id",))
            for name, target in keys
        },
    )


class TestPostgresDropOrder(unittest.TestCase):
    def generate(self, tables):
        snapshot = Snapshot(tables={"kept": table(), **tables}, constraints_read=True)
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            return autogenerate(
                None, [kept()], id="m", dialect=Dialects.POSTGRES, allow_drops=True
            )

    def test_a_chain_drops_from_the_far_end(self):
        migration = self.generate(
            {
                "a": table(),
                "b": table(("fk_b_a", "a")),
                "c": table(("fk_c_b", "b")),
            }
        )
        self.assertEqual(
            migration.up, ['DROP TABLE "c"', 'DROP TABLE "b"', 'DROP TABLE "a"']
        )

    def test_a_key_to_a_kept_table_or_itself_sets_no_order(self):
        migration = self.generate(
            {"a": table(("fk_a_kept", "kept"), ("fk_a_a", "a")), "b": table()}
        )
        self.assertEqual(migration.up, ['DROP TABLE "a"', 'DROP TABLE "b"'])

    def test_a_cycle_loses_its_keys_first(self):
        migration = self.generate(
            {
                "a": table(("fk_a_b", "b")),
                "b": table(("fk_b_a", "a")),
                "c": table(("fk_c_a", "a")),
            }
        )
        self.assertEqual(
            migration.up,
            [
                'ALTER TABLE "a" DROP CONSTRAINT "fk_a_b"',
                'ALTER TABLE "b" DROP CONSTRAINT "fk_b_a"',
                'DROP TABLE "b"',
                'DROP TABLE "c"',
                'DROP TABLE "a"',
            ],
        )
        self.assertTrue(migration.transactional)


if __name__ == "__main__":
    unittest.main()
