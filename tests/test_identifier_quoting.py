"""
Identifier quoting rules that hold for every dialect.

A quoted identifier must contain its own delimiter safely, and an alias
must be a plain name because the default dialect writes identifiers bare.
A column string can come from a request, so one that is not a column name
is refused rather than rendered.
"""

import unittest

from sustained import Model, QueryBuilder
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


class Item(Model):
    tableName = "items"


INJECTIONS = (
    "name UNION SELECT s FROM secrets --",
    "id = 0 OR 1",
    "id; DROP TABLE items",
    "COUNT(id) OR 1",
    "LOWER(name, 1)",
    "items.* , 1",
    "",
)


class TestColumnStrings(unittest.TestCase):
    """Every clause that takes a column string refuses SQL in it."""

    def tearDown(self):
        Item.set_dialect(Dialects.DEFAULT)

    def clauses(self, text):
        yield "select", lambda: Item.query().select(text)
        yield "where", lambda: Item.query().where(text, "=", 1)
        yield "whereIn", lambda: Item.query().whereIn(text, [1])
        yield "orderBy", lambda: Item.query().orderBy(text)
        yield "groupBy", lambda: Item.query().groupBy(text)
        yield "having", lambda: Item.query().groupBy("id").having(text, ">", 1)
        yield "distinctOn", lambda: Item.query().distinctOn(text)
        yield "from_", lambda: Item.query().from_(text)

    def test_sql_in_a_column_string_is_refused_on_every_dialect(self):
        for dialect in (Dialects.DEFAULT, Dialects.POSTGRES, Dialects.MYSQL):
            Item.set_dialect(dialect)
            for text in INJECTIONS:
                for clause, build in self.clauses(text):
                    with self.subTest(dialect=dialect.name, clause=clause, text=text):
                        with self.assertRaises(ValueError):
                            str(build())

    def test_from_takes_only_a_table_name(self):
        for text in ("items.*", "COUNT(id)", "*"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "not a table name"):
                    Item.query().from_(text)
        Item.set_dialect(Dialects.POSTGRES)
        self.assertEqual(
            str(Item.query().from_("sales.items")), 'SELECT * FROM "sales"."items"'
        )

    def test_error_names_the_string_and_the_raw_path(self):
        with self.assertRaises(ValueError) as caught:
            str(Item.query().orderBy("id DESC, (SELECT 1)"))
        self.assertIn("'id DESC, (SELECT 1)'", str(caught.exception))
        self.assertIn("QueryBuilder.raw()", str(caught.exception))

    def test_column_forms_are_quoted_per_dialect(self):
        Item.set_dialect(Dialects.POSTGRES)
        query = (
            Item.query()
            .select("items.*", "COUNT(*)", "count(DISTINCT items.maker_id) AS n")
            .groupBy("items.id")
            .having("SUM(items.price)", ">", 5)
            .orderBy("MAX(price)", "desc")
        )
        self.assertEqual(
            query.to_sql()[0],
            'SELECT "items".*, COUNT(*), count(DISTINCT "items"."maker_id") '
            'AS "n" FROM "items" GROUP BY "items"."id" '
            'HAVING SUM("items"."price") > %s ORDER BY MAX("price") DESC',
        )

    def test_raw_expressions_render_as_written(self):
        raw = QueryBuilder.raw("LOWER(name)")
        query = Item.query().select(raw).groupBy(raw).orderBy(raw)
        self.assertEqual(
            str(query),
            "SELECT LOWER(name) FROM items GROUP BY LOWER(name) "
            "ORDER BY LOWER(name) ASC",
        )


class TestSubqueryStrings(unittest.TestCase):
    """A string in subquery position is refused; raw() is the SQL path."""

    def clauses(self, text):
        yield "whereIn", lambda: Item.query().whereIn("id", text)
        yield "orWhereNotIn", lambda: Item.query().where("id", "=", 1).orWhereNotIn(
            "id", text
        )
        yield "havingIn", lambda: Item.query().groupBy("id").havingIn("id", text)
        yield "whereExists", lambda: Item.query().whereExists(text)
        yield "whereNotExists", lambda: Item.query().whereNotExists(text)
        yield "havingExists", lambda: Item.query().groupBy("id").havingExists(text)

    def test_a_string_is_refused_and_the_error_names_raw(self):
        for clause, build in self.clauses("0) OR (1=1"):
            with self.subTest(clause=clause):
                with self.assertRaises(ValueError) as caught:
                    build()
                self.assertIn("'0) OR (1=1'", str(caught.exception))
                self.assertIn("QueryBuilder.raw()", str(caught.exception))

    def test_raw_sql_renders_as_written(self):
        raw = QueryBuilder.raw("SELECT maker_id FROM makers")
        self.assertEqual(
            str(Item.query().whereIn("id", raw).orWhereNotExists(raw)),
            "SELECT * FROM items WHERE id IN (SELECT maker_id FROM makers) "
            "OR NOT EXISTS (SELECT maker_id FROM makers)",
        )

    def test_other_types_name_the_accepted_forms(self):
        with self.assertRaisesRegex(ValueError, "QueryBuilder.raw()"):
            Item.query().whereIn("id", 5)
        with self.assertRaisesRegex(ValueError, "QueryBuilder.raw()"):
            Item.query().whereExists(5)


class TestBareIdentifiers(unittest.TestCase):
    """The default dialect writes names bare, so it refuses one with SQL."""

    def test_default_dialect_refuses_a_name_that_is_not_plain(self):
        compiler = Dialects.get_compiler(Dialects.DEFAULT)
        for name in ("a b", "a;b", 'a"b', "1a", ""):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "DEFAULT dialect"):
                    compiler.quote_identifier(name)

    def test_join_table_and_on_columns_are_checked(self):
        with self.assertRaises(ValueError):
            str(Item.query().join("makers; DROP TABLE items", "a", "=", "b"))
        with self.assertRaises(ValueError):
            str(Item.query().join("makers", "makers.id", "=", "1 OR 1=1"))

    def test_on_takes_a_raw_expression_as_its_right_side(self):
        query = Item.query().join(
            "makers",
            lambda j: j.on("makers.id", "=", "items.maker_id").andOn(
                "makers.rank", ">", QueryBuilder.raw("10")
            ),
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM items JOIN makers ON makers.id = items.maker_id "
            "AND makers.rank > 10",
        )


if __name__ == "__main__":
    unittest.main()
