"""
Identifier quoting rules that hold for every dialect.

A quoted identifier must contain its own delimiter safely, and an alias
must be a plain name because the default dialect writes identifiers bare.
"""

import unittest

from sustained import Model
from sustained.dialects import Dialects
from sustained.expressions import (
    AggregateExpression,
    CaseExpression,
    Func,
    WindowExpression,
)


class TestDelimiterDoubling(unittest.TestCase):
    def test_double_quote_dialects_double_the_quote(self):
        for dialect in (Dialects.POSTGRES, Dialects.DUCKDB, Dialects.PRESTO):
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                self.assertEqual(compiler.quote_identifier('a"b'), '"a""b"')

    def test_mysql_doubles_the_backtick(self):
        compiler = Dialects.get_compiler(Dialects.MYSQL)
        self.assertEqual(compiler.quote_identifier("a`b"), "`a``b`")

    def test_mssql_doubles_the_closing_bracket(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(compiler.quote_identifier("a]b"), "[a]]b]")

    def test_mssql_qualified_name_doubles_each_part(self):
        compiler = Dialects.get_compiler(Dialects.MSSQL)
        self.assertEqual(
            compiler.quote_fully_qualified_identifier("dbo.a]b"),
            "[dbo].[a]]b]",
        )

    def test_athena_ddl_doubles_the_backtick(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertEqual(compiler.quote_ddl_identifier("a`b"), "`a``b`")

    def test_athena_queries_keep_double_quotes(self):
        compiler = Dialects.get_compiler(Dialects.ATHENA)
        self.assertEqual(compiler.quote_identifier('a"b'), '"a""b"')


class TestAliasValidation(unittest.TestCase):
    """An alias that is not a plain name is refused on every dialect."""

    def test_func_alias_with_quote_is_refused(self):
        for dialect in Dialects:
            with self.subTest(dialect=dialect.name):
                compiler = Dialects.get_compiler(dialect)
                with self.assertRaises(ValueError):
                    compiler.compile_function(Func("count", "*", alias='x") AS evil--'))

    def test_aggregate_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError):
            compiler.compile_aggregate(AggregateExpression("COUNT", "id", alias="a b"))

    def test_window_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError):
            compiler.compile_window(WindowExpression("ROW_NUMBER", "a-b"))

    def test_case_alias_with_quote_is_refused(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        case = CaseExpression("a;b", "no").when("1 = 1", "yes")
        with self.assertRaises(ValueError):
            compiler.compile_case(case)

    def test_plain_alias_is_quoted(self):
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertEqual(compiler.quote_alias("total_count"), '"total_count"')

    def test_error_names_the_alias(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        with self.assertRaises(ValueError) as caught:
            compiler.quote_alias("a b")
        self.assertIn("'a b'", str(caught.exception))


class TestQuotedNamesInStatements(unittest.TestCase):
    def test_table_name_with_a_quote_stays_inside_the_quotes(self):
        class Odd(Model):
            tableName = 'we"ird'

        Odd.set_dialect(Dialects.POSTGRES)
        try:
            self.assertEqual(str(Odd.query()), 'SELECT * FROM "we""ird"')
        finally:
            Odd.set_dialect(Dialects.DEFAULT)


if __name__ == "__main__":
    unittest.main()
