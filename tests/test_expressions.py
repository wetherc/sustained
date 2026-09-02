"""
Tests for the SQL expression classes.
"""

import unittest

from sustained.builder import QueryBuilder
from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
    Literal,
    Subquery,
    WindowExpression,
)
from sustained.model import Model


class TestAggregateExpression(unittest.TestCase):
    def test_basic_aggregate(self) -> None:
        """
        Tests a basic aggregate function without an alias.
        """
        agg = AggregateExpression("COUNT", "*")
        self.assertEqual(str(agg), "COUNT(*)")

    def test_aggregate_with_column(self) -> None:
        """
        Tests an aggregate function on a specific column.
        """
        agg = AggregateExpression("SUM", "price")
        self.assertEqual(str(agg), "SUM(price)")

    def test_aggregate_with_alias(self) -> None:
        """
        Tests an aggregate function with an alias.
        """
        agg = AggregateExpression("AVG", "quantity", "avg_quantity")
        self.assertEqual(str(agg), "AVG(quantity) AS avg_quantity")

    def test_aggregate_with_distinct(self) -> None:
        """
        Tests an aggregate function with DISTINCT in the column part.
        """
        agg = AggregateExpression("COUNT", "DISTINCT user_id", "unique_users")
        self.assertEqual(str(agg), "COUNT(DISTINCT user_id) AS unique_users")


class TestWindowExpression(unittest.TestCase):
    def test_basic_window_function(self) -> None:
        """
        The string form of a window carries no alias, because that form is
        the one used inside another expression.
        """
        window = WindowExpression("ROW_NUMBER", "row_num")
        self.assertEqual(str(window), "ROW_NUMBER() OVER ()")

    def test_window_with_partition_by(self) -> None:
        """
        Tests a window function with a PARTITION BY clause.
        """
        window = WindowExpression("RANK", "rank_val", partition_by=["category", "year"])
        self.assertEqual(str(window), "RANK() OVER (PARTITION BY category, year)")

    def test_window_with_order_by(self) -> None:
        """
        Tests a window function with an ORDER BY clause.
        """
        window = WindowExpression(
            "NTILE", "ntile_group", order_by=["score DESC", "date"]
        )
        self.assertEqual(str(window), "NTILE() OVER (ORDER BY score DESC, date)")

    def test_window_with_partition_and_order_by(self) -> None:
        """
        Tests a window function with both PARTITION BY and ORDER BY clauses.
        """
        window = WindowExpression(
            "LEAD",
            "next_value",
            partition_by=["product_id"],
            order_by=["transaction_date"],
        )
        self.assertEqual(
            str(window),
            "LEAD() OVER (PARTITION BY product_id ORDER BY transaction_date)",
        )

    def test_window_string_form_keeps_args_and_frame(self) -> None:
        """
        The string form keeps the function arguments and the frame clause,
        which the select-list form also keeps.
        """
        window = WindowExpression(
            "SUM",
            "running_total",
            order_by=["id"],
            args=["amount"],
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        )
        self.assertEqual(
            str(window),
            "SUM(amount) OVER (ORDER BY id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)",
        )

    def test_window_nested_in_a_function(self) -> None:
        """
        A window passed as a function argument keeps its arguments and its
        frame, and carries no alias inside the call.
        """
        window = WindowExpression(
            "SUM",
            "running_total",
            partition_by=["team_id"],
            order_by=["id"],
            args=["amount"],
            frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        )
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_function(Func("COALESCE", window, Literal(0))),
            "COALESCE(SUM(amount) OVER (PARTITION BY team_id ORDER BY id "
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0)",
        )

    def test_window_nested_in_a_function_quotes_per_dialect(self) -> None:
        """
        A nested window is quoted by the dialect that renders the call.
        """
        window = WindowExpression("RANK", "rank_val", partition_by=["team_id"])
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(
            compiler.compile_function(Func("CAST_TEST", window)),
            'CAST_TEST(RANK() OVER (PARTITION BY "team_id"))',
        )

    def test_window_in_a_select_list_keeps_its_alias(self) -> None:
        """
        The select-list form still ends with the alias.
        """
        window = WindowExpression("ROW_NUMBER", "row_num", order_by=["id"])
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_window(window),
            "ROW_NUMBER() OVER (ORDER BY id) AS row_num",
        )


class TestCaseExpression(unittest.TestCase):
    def test_basic_case_expression(self) -> None:
        """
        Tests a basic CASE expression with one WHEN clause.
        """
        case = (
            CaseExpression("status_desc", "Unknown")
            .when("status = 1", "Active")
            .when("status = 0", "Inactive")
        )
        self.assertEqual(
            str(case),
            "CASE WHEN status = 1 THEN 'Active' WHEN status = 0 THEN 'Inactive' ELSE 'Unknown' END AS status_desc",
        )

    def test_case_expression_with_multiple_whens(self) -> None:
        """
        Tests a CASE expression with multiple WHEN clauses.
        """
        case = (
            CaseExpression("grade_category", "F")
            .when("score >= 90", "A")
            .when("score >= 80", "B")
            .when("score >= 70", "C")
            .when("score >= 60", "D")
        )
        self.assertEqual(
            str(case),
            "CASE WHEN score >= 90 THEN 'A' WHEN score >= 80 THEN 'B' WHEN score >= 70 THEN 'C' WHEN score >= 60 THEN 'D' ELSE 'F' END AS grade_category",
        )

    def test_case_expression_with_numeric_else(self) -> None:
        """
        Tests a CASE expression where the ELSE result is numeric.
        """
        case = CaseExpression("discount_percent", 0).when("price > 100", 10)
        self.assertEqual(
            str(case), "CASE WHEN price > 100 THEN 10 ELSE 0 END AS discount_percent"
        )

    def test_case_expression_with_column_result(self) -> None:
        """
        Tests a CASE expression where the result is a column name.
        """
        case = CaseExpression("final_price", Column("price")).when(
            "promotion_active = TRUE", Column("discounted_price")
        )
        self.assertEqual(
            str(case),
            "CASE WHEN promotion_active = TRUE THEN discounted_price ELSE price END AS final_price",
        )

    def test_case_expression_escapes_a_quote_in_a_result(self) -> None:
        """
        A result holding a single quote is escaped, so the string form
        matches what the compiler produces.
        """
        case = CaseExpression("owner", "O'Brien").when("id = 1", "D'Arcy")
        self.assertEqual(
            str(case),
            "CASE WHEN id = 1 THEN 'D''Arcy' ELSE 'O''Brien' END AS owner",
        )

    def test_case_expression_nested_in_a_function_escapes(self) -> None:
        """
        A CASE passed as a function argument renders through the same
        escaping as a CASE in the select list.
        """
        case = CaseExpression("owner", "O'Brien").when("id = 1", "D'Arcy")
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        self.assertIn("'O''Brien'", compiler.compile_function(Func("UPPER", case)))

    def test_case_expression_refuses_a_non_identifier_alias(self) -> None:
        """
        The alias goes through the compiler's alias check, so it cannot
        carry SQL of its own.
        """
        case = CaseExpression('x") AS evil--', "a")
        with self.assertRaises(ValueError):
            str(case)


class TestSubqueryExpression(unittest.TestCase):
    def test_simple_subquery(self) -> None:
        """
        Tests a simple subquery expression.
        """

        class Tmp(Model): ...

        subquery_builder = (
            QueryBuilder(Tmp).select("id").from_("other_table").where("x", "=", 1)
        )
        subquery = Subquery(subquery_builder, "sub")
        self.assertEqual(
            str(subquery), "(SELECT id FROM other_table WHERE x = 1) AS sub"
        )

    def test_render_parameterizes_inner_values(self) -> None:
        """
        Tests that render() replaces the inner values with placeholders and
        collects them in the given context.
        """
        from sustained.dialects import Dialects
        from sustained.rendering import RenderContext

        class Tmp(Model): ...

        subquery_builder = (
            QueryBuilder(Tmp).select("id").from_("other_table").where("x", "=", 1)
        )
        subquery = Subquery(subquery_builder, "sub")
        ctx = RenderContext(Dialects.get_compiler(Dialects.DEFAULT), parameterize=True)
        self.assertEqual(
            subquery.render(ctx), "(SELECT id FROM other_table WHERE x = ?) AS sub"
        )
        self.assertEqual(ctx.params, [1])

    def test_render_operand_drops_the_alias(self) -> None:
        """
        Tests that render_operand() leaves the alias off, because an alias
        is not valid where the subquery stands as a value.
        """
        from sustained.dialects import Dialects
        from sustained.rendering import RenderContext

        class Tmp(Model): ...

        subquery_builder = (
            QueryBuilder(Tmp).select("id").from_("other_table").where("x", "=", 1)
        )
        subquery = Subquery(subquery_builder, "sub")
        ctx = RenderContext(Dialects.get_compiler(Dialects.DEFAULT), parameterize=True)
        self.assertEqual(
            subquery.render_operand(ctx), "(SELECT id FROM other_table WHERE x = ?)"
        )
        self.assertEqual(ctx.params, [1])

    def test_render_operand_inlines_without_a_context(self) -> None:
        """
        Tests that render_operand() inlines the inner values when no render
        context is available.
        """

        class Tmp(Model): ...

        subquery_builder = (
            QueryBuilder(Tmp).select("id").from_("other_table").where("x", "=", 1)
        )
        self.assertEqual(
            Subquery(subquery_builder, "sub").render_operand(None),
            "(SELECT id FROM other_table WHERE x = 1)",
        )

    def test_render_inlines_values_without_parameterization(self) -> None:
        """
        Tests that render() inlines the inner values when the context is not
        in parameterized mode.
        """
        from sustained.dialects import Dialects
        from sustained.rendering import RenderContext

        class Tmp(Model): ...

        subquery_builder = (
            QueryBuilder(Tmp).select("id").from_("other_table").where("x", "=", 1)
        )
        subquery = Subquery(subquery_builder, "sub")
        ctx = RenderContext(Dialects.get_compiler(Dialects.DEFAULT))
        self.assertEqual(
            subquery.render(ctx), "(SELECT id FROM other_table WHERE x = 1) AS sub"
        )
        self.assertEqual(ctx.params, [])
