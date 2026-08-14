import unittest

from sustained import Model, create_model
from sustained.dialects import Dialects
from sustained.exceptions import DialectError


class TestFunctionValidation(unittest.TestCase):
    def test_unsupported_function_raises_dialect_error(self):
        class User(Model):
            tableName = "users"

        # STRING_AGG is not supported by MSSQL
        User.set_dialect(Dialects.MSSQL)
        query = User.query()

        with self.assertRaisesRegex(
            DialectError,
            "Function 'STRING_AGG' is not supported by the 'MSSQL' dialect.",
        ):
            query.select_func("STRING_AGG", "name")

        # Reset dialect
        User.set_dialect(Dialects.DEFAULT)

    def test_unregistered_function_passes_through(self):
        class User(Model):
            tableName = "users"

        from sustained.expressions import Column

        query = User.query().select_func("my_awesome_func", Column("name"))

        self.assertIn("MY_AWESOME_FUNC(name)", str(query))


User = create_model("FuncSemanticsUser", "users")


class TestFunctionArgumentSemantics(unittest.TestCase):
    def test_string_args_are_columns(self):
        query = User.query().select_func("LOWER", "name", alias="n")
        self.assertEqual(str(query), "SELECT LOWER(name) AS n FROM users")

    def test_string_args_are_quoted_per_dialect(self):
        from sustained.dialects import Dialects

        Pg = create_model("FuncPg", "users")
        Pg.set_dialect(Dialects.POSTGRES)
        query = Pg.query().select_func("LOWER", "users.name", alias="n")
        self.assertEqual(str(query), 'SELECT LOWER("users"."name") AS "n" FROM "users"')

    def test_literal_wrapper_renders_literal(self):
        from sustained.expressions import Literal

        query = User.query().select_func("COALESCE", "nickname", Literal("N/A"))
        self.assertEqual(str(query), "SELECT COALESCE(nickname, 'N/A') FROM users")

    def test_non_identifier_string_rejected(self):
        query = User.query().select_func("LOWER", "not a column!")
        with self.assertRaises(ValueError):
            str(query)

    def test_numeric_args_render_as_literals(self):
        query = User.query().select_func("ROUND", "price", 2)
        self.assertEqual(str(query), "SELECT ROUND(price, 2) FROM users")
