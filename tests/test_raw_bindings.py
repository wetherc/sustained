"""
Tests for raw predicates with bound values.
"""

import unittest

from sustained import create_model
from sustained.dialects import Dialects

User = create_model("RawUser", "users")


class TestWhereRaw(unittest.TestCase):
    def test_markers_parameterize(self):
        sql, params = User.query().whereRaw("age % ? = ?", [2, 0]).to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE (age % ? = ?)")
        self.assertEqual(params, (2, 0))

    def test_inline_rendering_escapes(self):
        sql = str(User.query().whereRaw("name = ?", ["O'Brien"]))
        self.assertIn("(name = 'O''Brien')", sql)

    def test_no_params(self):
        sql = str(User.query().whereRaw("deleted_at IS NULL"))
        self.assertEqual(sql, "SELECT * FROM users WHERE (deleted_at IS NULL)")

    def test_marker_count_mismatch_raises(self):
        with self.assertRaises(ValueError):
            User.query().whereRaw("a = ?", [1, 2])
        with self.assertRaises(ValueError):
            User.query().whereRaw("a = ? AND b = ?", [1])

    def test_conjunctions(self):
        sql = str(User.query().where("a", "=", 1).orWhereRaw("b = ?", [2]))
        self.assertEqual(sql, "SELECT * FROM users WHERE a = 1 OR (b = 2)")

    def test_having_raw(self):
        sql, params = User.query().groupBy("x").havingRaw("COUNT(*) > ?", [5]).to_sql()
        self.assertEqual(sql, "SELECT * FROM users GROUP BY x HAVING (COUNT(*) > ?)")
        self.assertEqual(params, (5,))

    def test_postgres_placeholder_translation(self):
        Pg = create_model("RawPgUser", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql, params = Pg.query().whereRaw("age % ? = ?", [2, 0]).to_sql()
        self.assertEqual(sql, 'SELECT * FROM "users" WHERE (age % %s = %s)')
        self.assertEqual(params, (2, 0))

    def test_snake_alias(self):
        sql = str(User.query().where_raw("a = ?", [1]))
        self.assertEqual(sql, "SELECT * FROM users WHERE (a = 1)")


if __name__ == "__main__":
    unittest.main()
