"""
Tests for INSERT, UPDATE, and DELETE statement building.
"""

import unittest

from sustained import DialectError, Model, create_model
from sustained.dialects import Dialects

User = create_model("DmlUser", "users")


class TestInsert(unittest.TestCase):
    def test_single_row(self):
        sql, params = User.query().insert({"name": "a", "age": 3}).to_sql()
        self.assertEqual(sql, "INSERT INTO users (name, age) VALUES (?, ?)")
        self.assertEqual(params, ("a", 3))

    def test_multi_row(self):
        sql, params = User.query().insert([{"n": 1}, {"n": 2}]).to_sql()
        self.assertEqual(sql, "INSERT INTO users (n) VALUES (?), (?)")
        self.assertEqual(params, (1, 2))

    def test_inline_rendering(self):
        sql = str(User.query().insert({"name": "O'Brien"}))
        self.assertEqual(sql, "INSERT INTO users (name) VALUES ('O''Brien')")

    def test_mismatched_columns_raise(self):
        with self.assertRaises(ValueError):
            User.query().insert([{"a": 1}, {"b": 2}])

    def test_empty_rows_raise(self):
        with self.assertRaises(ValueError):
            User.query().insert([])
        with self.assertRaises(ValueError):
            User.query().insert({})

    def test_where_clause_rejected(self):
        query = User.query().insert({"a": 1}).where("id", "=", 1)
        with self.assertRaises(ValueError):
            str(query)

    def test_returning(self):
        sql, params = User.query().insert({"a": 1}).returning("id").to_sql()
        self.assertTrue(sql.endswith("RETURNING id"))

    def test_postgres_quoting(self):
        Pg = create_model("DmlPgUser", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql, params = Pg.query().insert({"name": "x"}).to_sql()
        self.assertEqual(sql, 'INSERT INTO "users" ("name") VALUES (%s)')


class TestUpdate(unittest.TestCase):
    def test_update_with_where(self):
        sql, params = (
            User.query().update({"name": "b", "age": 4}).where("id", "=", 1).to_sql()
        )
        self.assertEqual(sql, "UPDATE users SET name = ?, age = ? WHERE id = ?")
        self.assertEqual(params, ("b", 4, 1))

    def test_update_without_where_raises(self):
        with self.assertRaises(ValueError):
            str(User.query().update({"a": 1}))

    def test_update_requires_values(self):
        with self.assertRaises(ValueError):
            User.query().update({})


class TestDelete(unittest.TestCase):
    def test_delete_with_where(self):
        sql, params = User.query().delete().where("id", "=", 1).to_sql()
        self.assertEqual(sql, "DELETE FROM users WHERE id = ?")
        self.assertEqual(params, (1,))

    def test_delete_without_where_raises(self):
        with self.assertRaises(ValueError):
            str(User.query().delete())


class TestReturningDialects(unittest.TestCase):
    def test_mssql_returning_raises(self):
        Ms = create_model("DmlMsUser", "users")
        Ms.set_dialect(Dialects.MSSQL)
        with self.assertRaises(DialectError):
            str(Ms.query().insert({"a": 1}).returning("id"))

    def test_presto_returning_raises(self):
        Pr = create_model("DmlPrUser", "users")
        Pr.set_dialect(Dialects.PRESTO)
        with self.assertRaises(DialectError):
            str(Pr.query().insert({"a": 1}).returning("id"))


if __name__ == "__main__":
    unittest.main()
