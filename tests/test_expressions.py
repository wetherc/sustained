"""
Tests for the SQL expression classes.
"""

import unittest

from sustained.builder import QueryBuilder
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Column,
    Func,
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
        Tests a basic window function with only an alias and no partitions/orders.
        """
        window = WindowExpression("ROW_NUMBER", "row_num")
        self.assertEqual(str(window), "ROW_NUMBER() OVER () AS row_num")

    def test_window_with_partition_by(self) -> None:
        """
        Tests a window function with a PARTITION BY clause.
        """
        window = WindowExpression("RANK", "rank_val", partition_by=["category", "year"])
        self.assertEqual(
            str(window), "RANK() OVER (PARTITION BY category, year) AS rank_val"
        )

    def test_window_with_order_by(self) -> None:
        """
        Tests a window function with an ORDER BY clause.
        """
        window = WindowExpression(
            "NTILE", "ntile_group", order_by=["score DESC", "date"]
        )
        self.assertEqual(
            str(window), "NTILE() OVER (ORDER BY score DESC, date) AS ntile_group"
        )

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
            "LEAD() OVER (PARTITION BY product_id ORDER BY transaction_date) AS next_value",
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
