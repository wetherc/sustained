"""
Tests for the select-clause builder.
"""

import unittest

from sustained import create_model
from sustained.builders.select_clause_builder import SelectClauseBuilder
from sustained.dialects import Dialects
from sustained.expressions import Subquery
from sustained.rendering import RenderContext


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


class TestSelectClauseBuilderRender(unittest.TestCase):
    def _context(self, parameterize: bool = True) -> RenderContext:
        return RenderContext(
            Dialects.get_compiler(Dialects.DEFAULT), parameterize=parameterize
        )

    def test_render_matches_str_for_plain_columns(self) -> None:
        """
        Tests that render() and str() agree when nothing carries a value.
        """
        builder = SelectClauseBuilder()
        builder.select("id", "name AS handle")
        ctx = self._context()
        self.assertEqual(builder.render(ctx), str(builder))
        self.assertEqual(ctx.params, [])

    def test_render_defaults_to_star(self) -> None:
        """
        Tests that render() falls back to '*' with no selected columns.
        """
        self.assertEqual(SelectClauseBuilder().render(self._context()), "*")

    def test_render_parameterizes_a_subquery(self) -> None:
        """
        Tests that a subquery in the select list contributes its values to
        the context instead of inlining them.
        """
        model = create_model("SelectClauseUser", "users")
        inner = model.query().select("id").where("x", "=", 5)
        builder = SelectClauseBuilder()
        builder.select("name", Subquery(inner, "sub"))
        ctx = self._context()
        self.assertEqual(
            builder.render(ctx),
            "name, (SELECT id FROM users WHERE x = ?) AS sub",
        )
        self.assertEqual(ctx.params, [5])

    def test_str_inlines_a_subquery(self) -> None:
        """
        Tests that str() still renders a subquery with inline literals.
        """
        model = create_model("SelectClauseUserStr", "users")
        inner = model.query().select("id").where("x", "=", 5)
        builder = SelectClauseBuilder()
        builder.select("name", Subquery(inner, "sub"))
        self.assertEqual(
            str(builder), "name, (SELECT id FROM users WHERE x = 5) AS sub"
        )

    def test_render_uses_the_context_compiler(self) -> None:
        """
        Tests that render() quotes with the compiler the context carries.
        """
        builder = SelectClauseBuilder()
        builder.select("id")
        ctx = RenderContext(Dialects.get_compiler(Dialects.POSTGRES))
        self.assertEqual(builder.render(ctx), '"id"')
