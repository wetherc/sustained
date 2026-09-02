"""
Tests for parameterized rendering, operator validation, and CTE handling.
"""

import unittest

from sustained import DialectError, create_model
from sustained.dialects import Dialects
from sustained.expressions import Subquery

User = create_model("ParamUser", "users")


class TestToSql(unittest.TestCase):
    def test_simple_where(self):
        sql, params = User.query().select("id").where("age", ">", 21).to_sql()
        self.assertEqual(sql, "SELECT id FROM users WHERE age > ?")
        self.assertEqual(params, (21,))

    def test_params_in_sql_order(self):
        query = (
            User.query()
            .where("a", "=", 1)
            .whereIn("b", ["x", "y"])
            .whereBetween("c", 2, 3)
            .whereLike("d", "%z%")
        )
        sql, params = query.to_sql()
        self.assertEqual(params, (1, "x", "y", 2, 3, "%z%"))
        self.assertEqual(sql.count("?"), 6)

    def test_subquery_params_included(self):
        sub = User.query().select("id").where("z", "=", 7)
        sql, params = User.query().whereIn("id", sub).to_sql()
        self.assertEqual(
            sql, "SELECT * FROM users WHERE id IN (SELECT id FROM users WHERE z = ?)"
        )
        self.assertEqual(params, (7,))

    def test_select_list_subquery_params_included(self):
        sub = User.query().select("id").where("a", "=", 1)
        sql, params = (
            User.query().select("name", Subquery(sub, "s")).where("b", "=", 2).to_sql()
        )
        self.assertEqual(
            sql,
            "SELECT name, (SELECT id FROM users WHERE a = ?) AS s "
            "FROM users WHERE b = ?",
        )
        self.assertEqual(params, (1, 2))

    def test_select_list_subquery_params_follow_cte_params(self):
        cte = User.query().select("id").where("k", "=", 9)
        sub = User.query().select("id").where("a", "=", 1)
        query = (
            User.query()
            .with_("c", cte)
            .select("n", Subquery(sub, "s"))
            .where("b", "=", 2)
        )
        sql, params = query.to_sql()
        self.assertEqual(params, (9, 1, 2))
        self.assertTrue(sql.startswith("WITH c AS ("))

    def test_select_list_subquery_params_precede_from_subquery_params(self):
        sub = User.query().select("id").where("a", "=", 1)
        source = User.query().select("*").where("f", "=", 8)
        sql, params = (
            User.query()
            .select("p", Subquery(sub, "s"))
            .from_(source, alias="w")
            .to_sql()
        )
        self.assertEqual(params, (1, 8))
        self.assertLess(sql.index("AS s"), sql.index("AS w"))

    def test_select_list_subquery_params_across_union_members(self):
        first = User.query().select("x", Subquery(User.query().where("z", "=", 3), "t"))
        second = User.query().select(
            "x", Subquery(User.query().where("a", "=", 1), "s")
        )
        _, params = first.union(second).to_sql()
        self.assertEqual(params, (3, 1))

    def test_nested_select_list_subquery_params(self):
        innermost = User.query().select("id").where("deep", "=", 1)
        middle = User.query().select(Subquery(innermost, "d")).where("mid", "=", 2)
        sql, params = (
            User.query().select(Subquery(middle, "m")).where("top", "=", 3).to_sql()
        )
        self.assertEqual(params, (1, 2, 3))
        self.assertLess(sql.index("AS d"), sql.index("AS m"))

    def test_str_still_inlines_select_list_subquery_values(self):
        sub = User.query().select("id").where("a", "=", 1)
        self.assertEqual(
            str(User.query().select("name", Subquery(sub, "s"))),
            "SELECT name, (SELECT id FROM users WHERE a = 1) AS s FROM users",
        )

    def test_cte_params_precede_body_params(self):
        cte = User.query().select("id").where("k", "=", 9)
        query = User.query().with_("c", cte).where("a", "=", 1)
        sql, params = query.to_sql()
        self.assertEqual(params, (9, 1))
        self.assertTrue(sql.startswith("WITH c AS ("))

    def test_exists_subquery_params(self):
        sub = User.query().select("id").where("n", "=", 4)
        sql, params = User.query().whereExists(sub).to_sql()
        self.assertEqual(params, (4,))
        self.assertIn("EXISTS (", sql)

    def test_str_still_inlines_literals(self):
        query = User.query().where("name", "=", "O'Brien")
        self.assertIn("'O''Brien'", str(query))

    def test_postgres_placeholder(self):
        Pg = create_model("ParamPg", "pg_users")
        Pg.set_dialect(Dialects.POSTGRES)
        sql, params = Pg.query().where("a", "=", 1).to_sql()
        self.assertIn("%s", sql)
        self.assertNotIn("?", sql)


class TestOperatorValidation(unittest.TestCase):
    def test_injection_style_operator_rejected(self):
        with self.assertRaises(ValueError):
            User.query().where("id", "= 1 OR 1=1; --", 0)

    def test_operator_normalized(self):
        self.assertIn("WHERE a LIKE 'x'", str(User.query().where("a", "like", "x")))

    def test_non_string_operator_rejected(self):
        with self.assertRaises(TypeError):
            User.query().where("id", 5, 0)


class TestNoneValueHandling(unittest.TestCase):
    def test_equals_none_becomes_is_null(self):
        self.assertEqual(
            str(User.query().where("x", "=", None)),
            "SELECT * FROM users WHERE x IS NULL",
        )

    def test_not_equals_none_becomes_is_not_null(self):
        self.assertEqual(
            str(User.query().where("x", "!=", None)),
            "SELECT * FROM users WHERE x IS NOT NULL",
        )

    def test_none_with_inequality_operator_rejected(self):
        with self.assertRaises(ValueError):
            User.query().where("x", ">", None)


class TestUnionAndCteIntegrity(unittest.TestCase):
    def test_union_member_limit_preserved_where_parentheses_scope_it(self):
        Pg = create_model("ParamPgUser", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        member = Pg.query().select("id").limit(1)
        sql = str(Pg.query().select("id").union(member))
        self.assertIn('(SELECT "id" FROM "users" LIMIT 1)', sql)

    def test_union_member_limit_refused_where_members_render_bare(self):
        member = User.query().select("id").limit(1)
        with self.assertRaises(DialectError):
            str(User.query().select("id").union(member))

    def test_duplicate_cte_alias_conflict_raises(self):
        c1 = User.query().select("a")
        c2 = User.query().select("b")
        query = User.query().with_("c", c1).with_("c", c2)
        with self.assertRaises(ValueError):
            str(query)

    def test_duplicate_identical_cte_deduplicated(self):
        query = (
            User.query()
            .with_("c", User.query().select("a"))
            .with_("c", User.query().select("a"))
        )
        self.assertEqual(str(query).count("c AS ("), 1)

    def test_from_subquery_cte_hoisted(self):
        inner = User.query().with_("x", User.query().select("id")).select("*")
        sql = str(User.query().from_(inner, alias="s"))
        self.assertTrue(sql.startswith("WITH x AS (SELECT id FROM users)"))
        self.assertNotIn("(WITH", sql)

    def test_nested_cte_hoisted_in_dependency_order(self):
        innermost = User.query().select("id")
        middle = User.query().with_("inner_cte", innermost).select("*")
        sql = str(User.query().with_("outer_cte", middle).select("*"))
        self.assertLess(sql.index("inner_cte AS ("), sql.index("outer_cte AS ("))

    def test_with_requires_query_builder(self):
        with self.assertRaises(TypeError):
            User.query().with_("c", "SELECT 1")

    def test_empty_in_list_rejected(self):
        with self.assertRaises(ValueError):
            User.query().whereIn("id", [])


if __name__ == "__main__":
    unittest.main()
