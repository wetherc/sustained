"""
Percent signs in parameterized statements for the %s dialects.

psycopg, PyMySQL, and mysqlclient read a bare % in a statement with
parameters as the start of a placeholder, so to_sql() writes each literal
% sign as %% on Postgres and MySQL and leaves the placeholders alone.
"""

import unittest

from sustained import QueryBuilder, create_model
from sustained.aio import convert_format_to_numbered
from sustained.dialects import Dialects
from sustained.expressions import Func, Literal
from sustained.rendering import RenderContext

Item = create_model("PercentItem", "items")


def query(dialect: Dialects) -> QueryBuilder:
    return QueryBuilder(Item, dialect=dialect)


class TestPercentDoubling(unittest.TestCase):
    def test_raw_modulo_is_doubled_on_percent_dialects(self):
        for dialect, table in (
            (Dialects.POSTGRES, '"items"'),
            (Dialects.MYSQL, "`items`"),
        ):
            with self.subTest(dialect=dialect.name):
                sql, params = query(dialect).whereRaw("price % ? = ?", [10, 0]).to_sql()
                self.assertEqual(sql, f"SELECT * FROM {table} WHERE (price %% %s = %s)")
                self.assertEqual(params, (10, 0))

    def test_inlined_literal_is_doubled(self):
        sql, params = (
            query(Dialects.POSTGRES)
            .select(Func("CONCAT", "name", Literal("100%"), alias="label"))
            .to_sql()
        )
        self.assertEqual(
            sql, 'SELECT CONCAT("name", \'100%%\') AS "label" FROM "items"'
        )
        self.assertEqual(params, ())

    def test_bound_value_is_not_doubled(self):
        sql, params = query(Dialects.MYSQL).whereLike("name", "10%").to_sql()
        self.assertEqual(sql, "SELECT * FROM `items` WHERE `name` LIKE %s")
        self.assertEqual(params, ("10%",))

    def test_raw_percent_s_text_is_not_a_placeholder(self):
        sql, params = query(Dialects.POSTGRES).whereRaw("note = '%s'").to_sql()
        self.assertEqual(sql, "SELECT * FROM \"items\" WHERE (note = '%%s')")
        self.assertEqual(params, ())

    def test_str_keeps_one_percent_sign(self):
        text = str(query(Dialects.POSTGRES).whereRaw("price % ? = ?", [10, 0]))
        self.assertEqual(text, 'SELECT * FROM "items" WHERE (price % 10 = 0)')

    def test_question_mark_dialects_are_unchanged(self):
        for dialect in (Dialects.DEFAULT, Dialects.DUCKDB, Dialects.MSSQL):
            with self.subTest(dialect=dialect.name):
                sql, _ = query(dialect).whereRaw("price % ? = ?", [10, 0]).to_sql()
                self.assertIn("(price % ? = ?)", sql)

    def test_nul_in_text_raises(self):
        with self.assertRaisesRegex(ValueError, "NUL character"):
            query(Dialects.POSTGRES).whereRaw("note = '\x00'").to_sql()

    def test_finish_leaves_inline_text_alone(self):
        ctx = RenderContext(Dialects.get_compiler(Dialects.POSTGRES))
        self.assertEqual(ctx.finish("a % b"), "a % b")


class TestDriverReading(unittest.TestCase):
    def test_pymysql_formatting_reads_the_statement_back(self):
        sql, params = query(Dialects.MYSQL).whereRaw("price % ? = ?", [10, 0]).to_sql()
        # PyMySQL and mysqlclient apply Python's % operator to the
        # statement with the escaped parameters.
        self.assertEqual(
            sql % tuple(str(p) for p in params),
            "SELECT * FROM `items` WHERE (price % 10 = 0)",
        )

    def test_psycopg_reads_the_statement_back(self):
        try:
            from psycopg._queries import PostgresQuery
            from psycopg.adapt import Transformer
        except ImportError:
            self.skipTest("psycopg is not installed")
        sql, params = (
            query(Dialects.POSTGRES)
            .select(Func("CONCAT", "name", Literal("100%"), alias="label"))
            .whereRaw("price % ? = ?", [10, 0])
            .to_sql()
        )
        converted = PostgresQuery(Transformer())
        converted.convert(sql.encode(), params)
        self.assertEqual(
            converted.query,
            b'SELECT CONCAT("name", \'100%\') AS "label" FROM "items" '
            b"WHERE (price % $1 = $2)",
        )

    def test_asyncpg_conversion_reads_doubled_percent(self):
        self.assertEqual(
            convert_format_to_numbered("SELECT a %% %s, '100%%', b = %s"),
            "SELECT a % $1, '100%', b = $2",
        )


if __name__ == "__main__":
    unittest.main()
