import unittest

from sustained import Model
from sustained.dialects import Dialects
from sustained.exceptions import DialectError


class User(Model):
    tableName = "users"


class TestDialectFunctions(unittest.TestCase):
    def tearDown(self):
        # Reset dialect to default after each test
        User.set_dialect(Dialects.DEFAULT)

    def test_getdate_succeeds_with_mssql(self):
        User.set_dialect(Dialects.MSSQL)
        query = User.query().getdate(alias="current_time")
        self.assertEqual(str(query), "SELECT GETDATE() AS [current_time] FROM [users]")

    def test_getdate_fails_with_postgres(self):
        User.set_dialect(Dialects.POSTGRES)
        with self.assertRaisesRegex(
            DialectError,
            "Function 'GETDATE' is not supported by the 'POSTGRES' dialect.",
        ):
            User.query().getdate()

    def test_now_succeeds_with_postgres(self):
        User.set_dialect(Dialects.POSTGRES)
        query = User.query().now(alias="current_time")
        self.assertEqual(str(query), 'SELECT NOW() AS "current_time" FROM "users"')

    def test_now_fails_with_mssql(self):
        User.set_dialect(Dialects.MSSQL)
        with self.assertRaisesRegex(
            DialectError, "Function 'NOW' is not supported by the 'MSSQL' dialect."
        ):
            User.query().now()

    def test_coalesce_succeeds_with_all_dialects(self):
        from sustained.expressions import Column

        dialects = [
            Dialects.DEFAULT,
            Dialects.POSTGRES,
            Dialects.MSSQL,
            Dialects.PRESTO,
        ]
        for dialect in dialects:
            with self.subTest(dialect=dialect.name):
                User.set_dialect(dialect)
                query = User.query().coalesce(
                    Column("nickname"), "N/A", alias="display_name"
                )
                # We don't check the full string due to quoting differences,
                # just that the main parts are there.
                self.assertIn("COALESCE(nickname, 'N/A')", str(query))
                self.assertIn("AS", str(query))
                self.assertIn("display_name", str(query))

    def test_common_scalars_succeed_with_all_dialects(self):
        from sustained.expressions import Column

        # Test that common functions pass validation for all dialects
        common_scalars = [
            "LOWER",
            "UPPER",
            "CONCAT",
            "SUBSTRING",
            "TRIM",
            "LENGTH",
            "ROUND",
            "ABS",
            "CEILING",
            "FLOOR",
            "MOD",
        ]
        dialects = [
            Dialects.DEFAULT,
            Dialects.POSTGRES,
            Dialects.MSSQL,
            Dialects.PRESTO,
        ]

        for dialect in dialects:
            for func_name in common_scalars:
                with self.subTest(dialect=dialect.name, function=func_name):
                    User.set_dialect(dialect)
                    # Dynamically call the fluent method for the function
                    # The arguments here are placeholders for validation check
                    if func_name == "CONCAT":
                        query = getattr(User.query(), func_name.lower())("a", "b")
                    elif func_name == "SUBSTRING":
                        query = getattr(User.query(), func_name.lower())(
                            Column("name"), 1, 2
                        )
                    elif func_name == "ROUND":
                        query = getattr(User.query(), func_name.lower())(
                            Column("value"), 2
                        )
                    elif func_name == "MOD":
                        query = getattr(User.query(), func_name.lower())(
                            Column("value"), 2
                        )
                    else:
                        query = getattr(User.query(), func_name.lower())(Column("name"))

                    # We only need to check that this does not raise a DialectError.
                    # The rendering itself is tested elsewhere.
                    self.assertIn(func_name, str(query).upper())  # Basic check
