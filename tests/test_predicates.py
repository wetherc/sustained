"""
Tests for typed column expressions and composable predicates.
"""

import unittest

from sustained import Model, col, create_model
from sustained.dialects import Dialects


class PredUser(Model):
    tableName = "users"
    columns = ("id", "age", "name", "email", "active")


class TestColumnExprComparisons(unittest.TestCase):
    def test_comparison_operators(self):
        cases = [
            (PredUser.c.age > 21, "users.age > 21"),
            (PredUser.c.age >= 21, "users.age >= 21"),
            (PredUser.c.age < 21, "users.age < 21"),
            (PredUser.c.age <= 21, "users.age <= 21"),
            (PredUser.c.age == 21, "users.age = 21"),
            (PredUser.c.age != 21, "users.age != 21"),
        ]
        for predicate, expected in cases:
            with self.subTest(expected=expected):
                sql = str(PredUser.query().where(predicate))
                self.assertEqual(sql, f"SELECT * FROM users WHERE {expected}")

    def test_none_comparison_renders_null_checks(self):
        self.assertIn(
            "users.email IS NULL", str(PredUser.query().where(PredUser.c.email == None))
        )
        self.assertIn(
            "users.email IS NOT NULL",
            str(PredUser.query().where(PredUser.c.email != None)),
        )

    def test_none_with_inequality_raises(self):
        with self.assertRaises(ValueError):
            PredUser.c.age > None

    def test_column_to_column_comparison(self):
        sql = str(PredUser.query().where(PredUser.c.age == col("users.id")))
        self.assertIn("users.age = users.id", sql)

    def test_declared_columns_enforced(self):
        with self.assertRaises(AttributeError):
            PredUser.c.nope


class TestPredicateCombinators(unittest.TestCase):
    def test_and_or_not(self):
        predicate = (PredUser.c.age > 21) & (PredUser.c.name.like("A%")) | ~(
            PredUser.c.active == True  # noqa: E712
        )
        sql = str(PredUser.query().where(predicate))
        self.assertEqual(
            sql,
            "SELECT * FROM users WHERE ((users.age > 21 AND users.name LIKE 'A%') "
            "OR NOT (users.active = TRUE))",
        )

    def test_predicates_have_no_truth_value(self):
        with self.assertRaises(TypeError):
            bool(PredUser.c.age > 21)

    def test_predicate_with_operator_and_value_rejected(self):
        with self.assertRaises(ValueError):
            PredUser.query().where(PredUser.c.age > 21, "=", 1)


class TestColumnExprMethods(unittest.TestCase):
    def test_in_parameterizes(self):
        sql, params = PredUser.query().where(PredUser.c.id.in_([1, 2, 3])).to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE users.id IN (?, ?, ?)")
        self.assertEqual(params, (1, 2, 3))

    def test_empty_in_rejected(self):
        with self.assertRaises(ValueError):
            PredUser.c.id.in_([])

    def test_in_subquery(self):
        sub = PredUser.query().select("id")
        sql = str(PredUser.query().where(PredUser.c.id.not_in(sub)))
        self.assertIn("users.id NOT IN (SELECT id FROM users)", sql)

    def test_between(self):
        sql, params = PredUser.query().where(PredUser.c.age.between(18, 30)).to_sql()
        self.assertIn("users.age BETWEEN ? AND ?", sql)
        self.assertEqual(params, (18, 30))

    def test_not_between(self):
        sql = str(PredUser.query().where(PredUser.c.age.not_between(18, 30)))
        self.assertIn("users.age NOT BETWEEN 18 AND 30", sql)

    def test_like_variants(self):
        self.assertIn(
            "users.name LIKE 'A%'",
            str(PredUser.query().where(PredUser.c.name.like("A%"))),
        )
        self.assertIn(
            "users.name NOT LIKE 'A%'",
            str(PredUser.query().where(PredUser.c.name.not_like("A%"))),
        )

    def test_ilike_respects_dialect(self):
        Pg = create_model("PredPgUser", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        self.assertIn(
            '"users"."name" ILIKE %s',
            Pg.query().where(Pg.c.name.ilike("a%")).to_sql()[0],
        )
        self.assertIn(
            "LOWER(users.name) LIKE LOWER('a%')",
            str(PredUser.query().where(PredUser.c.name.ilike("a%"))),
        )

    def test_null_checks(self):
        self.assertIn(
            "users.email IS NULL",
            str(PredUser.query().where(PredUser.c.email.is_null())),
        )
        self.assertIn(
            "users.email IS NOT NULL",
            str(PredUser.query().where(PredUser.c.email.not_null())),
        )


class TestPredicateQuoting(unittest.TestCase):
    def test_columns_quote_per_dialect(self):
        Pg = create_model("PredQuotePg", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql = str(Pg.query().where(Pg.c.age > 21))
        self.assertIn('"users"."age" > 21', sql)

    def test_predicate_in_having(self):
        sql = str(PredUser.query().groupBy("name").having(col("COUNT(*)") > 5))
        self.assertIn("HAVING COUNT(*) > 5", sql)

    def test_col_helper_usable_in_select(self):
        sql = str(PredUser.query().select(col("users.name")))
        self.assertEqual(sql, "SELECT users.name FROM users")


if __name__ == "__main__":
    unittest.main()
