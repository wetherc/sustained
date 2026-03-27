import unittest
from unittest.mock import MagicMock

from sustained.builders.select_clause_builder import SelectClauseBuilder
from sustained.expressions import SelectExpression


class MockSelectExpression(SelectExpression):
    def __init__(self, sql):
        self._sql = sql

    def __str__(self):
        return self._sql


class TestSelectClauseBuilder(unittest.TestCase):
    def setUp(self):
        self.model_class = MagicMock()
        self.builder = SelectClauseBuilder(self.model_class)

    def test_default_select(self):
        self.assertEqual(str(self.builder), "*")

    def test_select_single_column(self):
        self.builder.select("id")
        self.assertEqual(str(self.builder), "id")

    def test_select_multiple_columns(self):
        self.builder.select("id", "name")
        self.assertEqual(str(self.builder), "id, name")

    def test_select_mixed_strings_and_expressions(self):
        expr = MockSelectExpression("COUNT(*)")
        self.builder.select("id", expr)
        self.assertEqual(str(self.builder), "id, COUNT(*)")

    def test_select_multiple_calls(self):
        self.builder.select("id")
        self.builder.select("name")
        self.assertEqual(str(self.builder), "id, name")


if __name__ == "__main__":
    unittest.main()
