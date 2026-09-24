"""
Tables and columns named after SQL keywords, through every DDL path the
default dialect generates on SQLite: create, add, rebuild, and drop.
"""

import sqlite3
import unittest
from unittest import mock

from sustained import create_model
from sustained.autogenerate import autogenerate, diff_schema
from sustained.dialects import Dialects
from sustained.introspect import IntrospectedColumn, IntrospectedTable, Snapshot
from sustained.schema import Index, Integer, String, Text


def keyword_model(columns, indexes=None):
    model = create_model("KeywordGroup", "group")
    model.tableColumns = columns
    model.columns = tuple(columns)
    model.indexes = indexes or []
    return model


def run(conn, migration, direction="up"):
    for statement in getattr(migration, direction):
        conn.execute(statement)


class TestKeywordNamesOnSqlite(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.base = {"id": Integer(primary_key=True), "order": String(20)}

    def test_create_and_drop_round_trip(self):
        model = keyword_model(self.base, [Index("select", "order")])
        migration = autogenerate(self.conn, [model], id="create")
        run(self.conn, migration)
        self.assertTrue(diff_schema(self.conn, [model]).is_empty())
        run(self.conn, migration, "down")
        tables = self.conn.execute("SELECT name FROM sqlite_master").fetchall()
        self.assertEqual(tables, [])

    def test_added_column_and_its_down(self):
        run(self.conn, autogenerate(self.conn, [keyword_model(self.base)], id="a"))
        wider = keyword_model({**self.base, "where": Text()})
        migration = autogenerate(self.conn, [wider], id="b")
        run(self.conn, migration)
        self.assertTrue(diff_schema(self.conn, [wider]).is_empty())
        run(self.conn, migration, "down")
        self.assertTrue(diff_schema(self.conn, [keyword_model(self.base)]).is_empty())

    def test_rebuild_copies_the_rows(self):
        model = keyword_model(self.base, [Index("select", "order")])
        run(self.conn, autogenerate(self.conn, [model], id="a"))
        self.conn.execute("""INSERT INTO "group" ("id", "order") VALUES (1, 'x')""")
        self.conn.execute('ALTER TABLE "group" ADD COLUMN "from" TEXT')
        self.conn.execute("""UPDATE "group" SET "from" = 'kept'""")
        narrowed = keyword_model(
            {"id": Integer(primary_key=True), "order": String(20, nullable=False)},
            [Index("select", "order")],
        )
        migration = autogenerate(self.conn, [narrowed], id="b", ignore_undeclared=True)
        run(self.conn, migration)
        rows = self.conn.execute('SELECT "id", "order", "from" FROM "group"')
        self.assertEqual(rows.fetchall(), [(1, "x", "kept")])

    def test_dropped_column_and_table(self):
        run(self.conn, autogenerate(self.conn, [keyword_model(self.base)], id="a"))
        self.conn.execute('ALTER TABLE "group" ADD COLUMN "from" TEXT')
        self.conn.execute('CREATE TABLE "table" ("id" INTEGER)')
        migration = autogenerate(
            self.conn, [keyword_model(self.base)], id="b", allow_drops=True
        )
        run(self.conn, migration)
        self.assertTrue(diff_schema(self.conn, [keyword_model(self.base)]).is_empty())


class TestGeneratedDialectQuotes(unittest.TestCase):
    """
    A model bound to the default dialect and generated for another one
    quotes its table the way the target dialect does.
    """

    def test_mysql_generation_quotes_with_backticks(self):
        model = keyword_model({"id": Integer(primary_key=True), "bio": Text()})
        snapshot = Snapshot(
            tables={
                "group": IntrospectedTable(
                    columns={
                        "id": IntrospectedColumn("int", False, True),
                    },
                    primary_key=("id",),
                )
            }
        )
        with mock.patch(
            "sustained.autogenerate.introspect_schema", return_value=snapshot
        ):
            migration = autogenerate(None, [model], id="m", dialect=Dialects.MYSQL)
        self.assertEqual(migration.up, ["ALTER TABLE `group` ADD COLUMN `bio` TEXT"])


if __name__ == "__main__":
    unittest.main()
