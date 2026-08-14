"""
Tests for INSERT ... SELECT and CREATE TABLE AS statements.
"""

import sqlite3
import unittest

from sustained import DialectError, Model, create_model
from sustained.dialects import Dialects

User = create_model("EtlUser", "users")
Archive = create_model("EtlArchive", "archive")


class TestInsertFromRendering(unittest.TestCase):
    def test_with_columns(self):
        source = User.query().select("id", "name").where("active", "=", False)
        sql, params = Archive.query().insert_from(["id", "name"], source).to_sql()
        self.assertEqual(
            sql,
            "INSERT INTO archive (id, name) "
            "SELECT id, name FROM users WHERE active = ?",
        )
        self.assertEqual(params, (False,))

    def test_without_columns(self):
        source = User.query().select("id", "name")
        sql = str(Archive.query().insert_from(None, source))
        self.assertEqual(sql, "INSERT INTO archive SELECT id, name FROM users")

    def test_requires_query_builder(self):
        with self.assertRaises(TypeError):
            Archive.query().insert_from(["id"], "SELECT 1")

    def test_where_on_outer_rejected(self):
        source = User.query().select("id")
        query = Archive.query().insert_from(["id"], source).where("x", "=", 1)
        with self.assertRaises(ValueError):
            str(query)

    def test_source_cte_renders(self):
        cte = User.query().select("id").where("active", "=", True)
        source = User.query().with_("act", cte).from_("act").select("id")
        sql = str(Archive.query().insert_from(["id"], source))
        self.assertIn("INSERT INTO archive (id) WITH act AS (", sql)


class TestCtasRendering(unittest.TestCase):
    def test_basic_ctas(self):
        sql = str(User.query().select("id").create_table_as("ids"))
        self.assertEqual(sql, "CREATE TABLE ids AS SELECT id FROM users")

    def test_temporary_ctas(self):
        sql = str(User.query().select("id").create_table_as("ids", temporary=True))
        self.assertTrue(sql.startswith("CREATE TEMPORARY TABLE ids AS"))

    def test_ctas_quotes_table_per_dialect(self):
        Pg = create_model("EtlPg", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql = str(Pg.query().select("id").create_table_as("analytics.ids"))
        self.assertIn('CREATE TABLE "analytics"."ids" AS', sql)

    def test_ctas_requires_select(self):
        with self.assertRaises(ValueError):
            User.query().insert({"a": 1}).create_table_as("x")

    def test_ctas_requires_name(self):
        with self.assertRaises(ValueError):
            User.query().create_table_as("")

    def test_mssql_ctas_raises(self):
        Ms = create_model("EtlMs", "users")
        Ms.set_dialect(Dialects.MSSQL)
        with self.assertRaises(DialectError):
            str(Ms.query().select("id").create_table_as("x"))


class TestEtlExecution(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (id INTEGER, name TEXT, active INTEGER)")
        self.conn.execute("CREATE TABLE archive (id INTEGER, name TEXT)")

        class LiveUser(Model):
            tableName = "users"

        class LiveArchive(Model):
            tableName = "archive"

        self.LiveUser = LiveUser
        self.LiveArchive = LiveArchive
        LiveUser.bind(self.conn)
        LiveArchive.bind(self.conn)
        LiveUser.query().insert(
            [
                {"id": 1, "name": "a", "active": 0},
                {"id": 2, "name": "b", "active": 1},
            ]
        ).run()

    def tearDown(self):
        self.LiveUser.unbind()
        self.LiveArchive.unbind()
        self.conn.close()

    def test_insert_from_copies_rows(self):
        source = self.LiveUser.query().select("id", "name").where("active", "=", 0)
        copied = self.LiveArchive.query().insert_from(["id", "name"], source).run()
        self.assertEqual(copied, 1)
        rows = self.conn.execute("SELECT id, name FROM archive").fetchall()
        self.assertEqual(rows, [(1, "a")])

    def test_ctas_creates_table(self):
        self.LiveUser.query().select("id").where("active", "=", 1).create_table_as(
            "active_ids"
        ).run()
        rows = self.conn.execute("SELECT id FROM active_ids").fetchall()
        self.assertEqual(rows, [(2,)])


if __name__ == "__main__":
    unittest.main()
