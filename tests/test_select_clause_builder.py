"""
Tests for the select-clause builder.
"""

import unittest

from sustained.builders.select_clause_builder import SelectClauseBuilder


class MockExpression:
    def __init__(self, sql: str):
        self._sql = sql

    def __str__(self) -> str:
        return self._sql


class TestSelectClauseBuilder(unittest.TestCase):
    def test_default_select(self) -> None:
        """
        Tests that the builder defaults to '*' when no columns are selected.
        """
        builder = SelectClauseBuilder()
        self.assertEqual(str(builder), "*")

    def test_simple_string_columns(self) -> None:
        """
        Tests selecting a list of simple string columns.
        """
        builder = SelectClauseBuilder()
        builder.select("id", "name", "email")
        self.assertEqual(str(builder), "id, name, email")

    def test_mixed_columns_and_expressions(self) -> None:
        """
        Tests selecting a mix of strings and complex expression objects.
        """
        builder = SelectClauseBuilder()
        builder.select(
            "id",
            MockExpression("COUNT(*)"),
            "name",
            MockExpression("SUM(price) AS total_price"),
        )
        self.assertEqual(str(builder), "id, COUNT(*), name, SUM(price) AS total_price")

    def test_adding_columns_incrementally(self) -> None:
        """
        Tests adding columns in multiple calls to select().
        """
        builder = SelectClauseBuilder()
        builder.select("id")
        builder.select("name", "email")
        builder.select(MockExpression("COUNT(*)"))
        self.assertEqual(str(builder), "id, name, email, COUNT(*)")
