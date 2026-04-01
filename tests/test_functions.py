import unittest

from sustained import Model
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
