"""
Tests for insert conflict handling: onConflict().merge() and ignore().
"""

import sqlite3
import unittest

from sustained import DialectError, Model, create_model
from sustained.dialects import Dialects

User = create_model("UpsertUser", "users")


class TestUpsertRendering(unittest.TestCase):
    def test_merge_renders_on_conflict_update(self):
        sql, params = (
            User.query()
            .insert({"email": "a@x", "name": "A"})
            .onConflict("email")
            .merge()
            .to_sql()
        )
        self.assertEqual(
            sql,
            "INSERT INTO users (email, name) VALUES (?, ?) "
            "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name",
        )
        self.assertEqual(params, ("a@x", "A"))

    def test_merge_with_explicit_columns(self):
        sql = str(
            User.query()
            .insert({"email": "a@x", "name": "A", "seen": 1})
            .onConflict("email")
            .merge(["seen"])
        )
        self.assertTrue(sql.endswith("DO UPDATE SET seen = EXCLUDED.seen"))

    def test_ignore_renders_do_nothing(self):
        sql = str(User.query().insert({"email": "a@x"}).onConflict("email").ignore())
        self.assertTrue(sql.endswith("ON CONFLICT (email) DO NOTHING"))

    def test_mssql_renders_merge_statement(self):
        Ms = create_model("UpsertMs", "t")
        Ms.set_dialect(Dialects.MSSQL)
        sql = str(Ms.query().insert({"k": 1, "v": 2}).onConflict("k").merge())
        self.assertTrue(sql.startswith("MERGE INTO [t] AS target USING (VALUES"))
        self.assertIn("WHEN MATCHED THEN UPDATE SET target.[v] = source.[v]", sql)
        self.assertTrue(sql.endswith(";"))

    def test_mssql_ignore_omits_matched_clause(self):
        Ms = create_model("UpsertMsIgnore", "t")
        Ms.set_dialect(Dialects.MSSQL)
        sql = str(Ms.query().insert({"k": 1, "v": 2}).onConflict("k").ignore())
        self.assertNotIn("WHEN MATCHED", sql)
        self.assertIn("WHEN NOT MATCHED THEN INSERT", sql)

    def test_presto_raises(self):
        Pr = create_model("UpsertPr", "t")
        Pr.set_dialect(Dialects.PRESTO)
        with self.assertRaises(DialectError):
            str(Pr.query().insert({"k": 1}).onConflict("k").ignore())


class TestUpsertValidation(unittest.TestCase):
    def test_on_conflict_requires_insert(self):
        with self.assertRaises(ValueError):
            User.query().onConflict("email")

    def test_conflict_column_must_be_inserted(self):
        with self.assertRaises(ValueError):
            User.query().insert({"name": "A"}).onConflict("email")

    def test_merge_requires_on_conflict(self):
        with self.assertRaises(ValueError):
            User.query().insert({"a": 1}).merge()

    def test_ignore_requires_on_conflict(self):
        with self.assertRaises(ValueError):
            User.query().insert({"a": 1}).ignore()

    def test_action_required_at_render(self):
        query = User.query().insert({"email": "a@x"}).onConflict("email")
        with self.assertRaises(ValueError):
            str(query)

    def test_merge_with_only_key_columns_raises(self):
        query = User.query().insert({"email": "a@x"}).onConflict("email").merge()
        with self.assertRaises(ValueError):
            str(query)


class TestUpsertExecution(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT)")

        class LiveUser(Model):
            tableName = "users"

        self.LiveUser = LiveUser
        LiveUser.bind(self.conn)

    def tearDown(self):
        self.LiveUser.unbind()
        self.conn.close()

    def name_of(self, email):
        row = self.conn.execute(
            "SELECT name FROM users WHERE email = ?", (email,)
        ).fetchone()
        return row[0] if row else None

    def test_merge_updates_existing_row(self):
        q = self.LiveUser.query()
        q.insert({"email": "a@x", "name": "A"}).run()
        self.LiveUser.query().insert({"email": "a@x", "name": "B"}).onConflict(
            "email"
        ).merge().run()
        self.assertEqual(self.name_of("a@x"), "B")

    def test_ignore_keeps_existing_row(self):
        self.LiveUser.query().insert({"email": "a@x", "name": "A"}).run()
        self.LiveUser.query().insert({"email": "a@x", "name": "C"}).onConflict(
            "email"
        ).ignore().run()
        self.assertEqual(self.name_of("a@x"), "A")

    def test_multi_row_upsert_through_batch_path(self):
        self.LiveUser.query().insert({"email": "a@x", "name": "A"}).run()
        self.LiveUser.query().insert(
            [{"email": "b@x", "name": "B"}, {"email": "a@x", "name": "D"}]
        ).onConflict("email").merge().run()
        self.assertEqual(self.name_of("a@x"), "D")
        self.assertEqual(self.name_of("b@x"), "B")


if __name__ == "__main__":
    unittest.main()
