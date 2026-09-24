"""
Rename hints and the foreign keys of other tables. The engine points a
child's key at a renamed parent table or column, so the diff reads the
key the same way and reports no change to it.
"""

import sqlite3
import unittest

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.schema import ForeignKey, Integer


def parent(table="makers", key="id"):
    model = create_model("RenameParent", table)
    model.tableColumns = {key: Integer(primary_key=True)}
    model.columns = tuple(model.tableColumns)
    return model


def child(target):
    model = create_model("RenameChild", "widgets")
    model.tableColumns = {"id": Integer(primary_key=True), "maker_id": Integer()}
    model.tableConstraints = [ForeignKey("fk_widgets_maker", "maker_id", target)]
    model.columns = tuple(model.tableColumns)
    return model


class TestRenamedTargets(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE makers (id INTEGER PRIMARY KEY)")
        self.conn.execute(
            "CREATE TABLE widgets (id INTEGER PRIMARY KEY, maker_id INTEGER, "
            "CONSTRAINT fk_widgets_maker FOREIGN KEY (maker_id) "
            "REFERENCES makers (id))"
        )

    def apply(self, models, **hints):
        migration = autogenerate(self.conn, models, id="m", **hints)
        for statement in migration.up:
            self.conn.execute(statement)
        self.assertTrue(diff_schema(self.conn, models).is_empty())
        return migration

    def test_a_renamed_parent_table_leaves_the_key_alone(self):
        models = [parent("vendors"), child("vendors.id")]
        diff = diff_schema(self.conn, models, table_renames={"makers": "vendors"})
        self.assertTrue(diff.is_empty(), diff.summary())
        migration = self.apply(models, table_renames={"makers": "vendors"})
        self.assertEqual(migration.up, ['ALTER TABLE "makers" RENAME TO "vendors"'])

    def test_a_renamed_parent_column_leaves_the_key_alone(self):
        models = [parent(key="maker_key"), child("makers.maker_key")]
        diff = diff_schema(self.conn, models, renames={"makers.id": "maker_key"})
        self.assertTrue(diff.is_empty(), diff.summary())
        self.apply(models, renames={"makers.id": "maker_key"})

    def test_a_key_that_points_elsewhere_is_untouched(self):
        self.conn.execute("CREATE TABLE owners (id INTEGER PRIMARY KEY)")
        owners = create_model("RenameOwner", "owners")
        owners.tableColumns = {"id": Integer(primary_key=True)}
        owners.columns = ("id",)
        diff = diff_schema(
            self.conn,
            [parent("vendors"), owners, child("owners.id")],
            table_renames={"makers": "vendors"},
        )
        ((_, fk, actual),) = diff.changed_foreign_keys
        self.assertEqual((fk.target_table, actual.target_table), ("owners", "vendors"))


if __name__ == "__main__":
    unittest.main()
