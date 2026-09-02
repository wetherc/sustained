"""
Tests for parameterized rendering, operator validation, and CTE handling.
"""

import unittest

from sustained import DialectError, create_model
from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    Column,
    Func,
    Literal,
    Subquery,
    col,
)

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


class TestFunctionArgumentParameters(unittest.TestCase):
    """A subquery inside a function call parameterizes with the statement."""

    def test_select_list_function_subquery_params(self):
        sub = User.query().select("id").where("a", "=", 1)
        query = User.query().select_func(
            "COALESCE", Subquery(sub, "s"), Literal(0), alias="c"
        )
        sql, params = query.to_sql()
        self.assertEqual(
            sql,
            "SELECT COALESCE((SELECT id FROM users WHERE a = ?), 0) AS c FROM users",
        )
        self.assertEqual(params, (1,))

    def test_select_list_function_params_sit_between_the_other_bindings(self):
        cte = User.query().select("id").where("k", "=", 9)
        sub = User.query().select("id").where("a", "=", 1)
        query = (
            User.query()
            .with_("c", cte)
            .select_func("COALESCE", Subquery(sub, "s"), Literal(0))
            .where("b", "=", 2)
        )
        sql, params = query.to_sql()
        self.assertEqual(params, (9, 1, 2))
        self.assertLess(sql.index("COALESCE"), sql.index("WHERE b"))

    def test_where_clause_function_subquery_params(self):
        sub = User.query().select("id").where("a", "=", 1)
        query = User.query().where(
            col("total") > Func("COALESCE", Subquery(sub, "s"), Literal(0))
        )
        sql, params = query.to_sql()
        self.assertEqual(
            sql,
            "SELECT * FROM users WHERE total > "
            "COALESCE((SELECT id FROM users WHERE a = ?), 0)",
        )
        self.assertEqual(params, (1,))

    def test_where_clause_function_params_sit_between_the_other_bindings(self):
        sub = User.query().select("id").where("a", "=", 2)
        query = (
            User.query()
            .where("x", "=", 1)
            .andWhere(col("total") > Func("COALESCE", Subquery(sub, "s"), Literal(0)))
            .andWhere("y", "=", 3)
        )
        sql, params = query.to_sql()
        self.assertEqual(params, (1, 2, 3))
        self.assertLess(sql.index("COALESCE"), sql.index("y ="))

    def test_nested_function_subquery_params(self):
        inner = User.query().select("id").where("deep", "=", 1)
        query = User.query().select_func(
            "UPPER", Func("COALESCE", Subquery(inner, "s"), Literal("x"))
        )
        _, params = query.to_sql()
        self.assertEqual(params, (1,))

    def test_window_argument_subquery_params(self):
        sub = User.query().select("id").where("a", "=", 5)
        query = User.query().select_window(
            "SUM", "total", partition_by=["dept"], args=[Subquery(sub, "s")]
        )
        sql, params = query.to_sql()
        self.assertIn("SUM((SELECT id FROM users WHERE a = ?))", sql)
        self.assertEqual(params, (5,))

    def test_str_still_inlines_a_function_subquery(self):
        sub = User.query().select("id").where("a", "=", 1)
        query = User.query().select_func("COALESCE", Subquery(sub, "s"), Literal(0))
        self.assertEqual(
            str(query),
            "SELECT COALESCE((SELECT id FROM users WHERE a = 1), 0) FROM users",
        )

    def test_union_members_keep_function_params_in_order(self):
        first = User.query().select_func(
            "COALESCE", Subquery(User.query().where("z", "=", 3), "t"), Literal(0)
        )
        second = User.query().select_func(
            "COALESCE", Subquery(User.query().where("a", "=", 1), "s"), Literal(0)
        )
        _, params = first.union(second).to_sql()
        self.assertEqual(params, (3, 1))

    def test_having_clause_function_subquery_params(self):
        sub = User.query().select("id").where("a", "=", 7)
        query = (
            User.query()
            .where("x", "=", 1)
            .groupBy("dept")
            .having(col("n") > Func("COALESCE", Subquery(sub, "s"), Literal(0)))
        )
        _, params = query.to_sql()
        self.assertEqual(params, (1, 7))


class TestExpressionOperands(unittest.TestCase):
    """An expression on the value side of a comparison renders as SQL."""

    def test_column_operand_renders_as_sql(self):
        query = User.query().where("show_id", "=", Column("shows.id"))
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE show_id = shows.id")
        self.assertEqual(params, ())

    def test_literal_operand_binds_its_value(self):
        sql, params = User.query().where("name", "=", Literal("ann")).to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE name = ?")
        self.assertEqual(params, ("ann",))

    def test_aggregate_operand_renders_as_sql(self):
        query = User.query().where("n", "=", AggregateExpression("COUNT", "id"))
        self.assertEqual(str(query), "SELECT * FROM users WHERE n = COUNT(id)")

    def test_between_operands_render_as_sql(self):
        query = User.query().whereBetween("n", Column("low"), 10)
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE n BETWEEN low AND ?")
        self.assertEqual(params, (10,))

    def test_in_list_operand_renders_as_sql(self):
        query = User.query().whereIn("n", [Column("a"), 2])
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE n IN (a, ?)")
        self.assertEqual(params, (2,))

    def test_predicate_in_list_operand_renders_as_sql(self):
        query = User.query().where(col("n").in_([Column("a"), 2]))
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE n IN (a, ?)")
        self.assertEqual(params, (2,))

    def test_predicate_between_operands_render_as_sql(self):
        query = User.query().where(col("n").between(Column("low"), 10))
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE n BETWEEN low AND ?")
        self.assertEqual(params, (10,))

    def test_predicate_not_between_operands_render_as_sql(self):
        query = User.query().where(col("n").not_between(1, Column("high")))
        sql, params = query.to_sql()
        self.assertEqual(sql, "SELECT * FROM users WHERE n NOT BETWEEN ? AND high")
        self.assertEqual(params, (1,))

    def test_subquery_operand_renders_without_its_alias(self):
        sub = User.query().select("id").where("a", "=", 4)
        query = User.query().where("n", "=", Subquery(sub, "s"))
        sql, params = query.to_sql()
        self.assertEqual(
            sql,
            "SELECT * FROM users WHERE n = (SELECT id FROM users WHERE a = ?)",
        )
        self.assertEqual(params, (4,))

    def test_like_operand_renders_as_sql(self):
        query = User.query().where("name", "LIKE", Column("pattern"))
        self.assertEqual(str(query), "SELECT * FROM users WHERE name LIKE pattern")


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
