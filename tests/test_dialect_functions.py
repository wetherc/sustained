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

    def test_getdate_translates_to_now_on_postgres(self):
        User.set_dialect(Dialects.POSTGRES)
        query = User.query().getdate(alias="current_time")
        self.assertEqual(str(query), 'SELECT NOW() AS "current_time" FROM "users"')

    def test_now_succeeds_with_postgres(self):
        User.set_dialect(Dialects.POSTGRES)
        query = User.query().now(alias="current_time")
        self.assertEqual(str(query), 'SELECT NOW() AS "current_time" FROM "users"')

    def test_now_translates_to_getdate_on_mssql(self):
        User.set_dialect(Dialects.MSSQL)
        query = User.query().now(alias="current_time")
        self.assertEqual(str(query), "SELECT GETDATE() AS [current_time] FROM [users]")

    def test_coalesce_succeeds_with_all_dialects(self):
        from sustained.expressions import Column, Literal

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
                    Column("nickname"), Literal("N/A"), alias="display_name"
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

                    # We only need to check that this does not raise a
                    # DialectError. LENGTH renders as LEN on MSSQL.
                    expected = func_name
                    if func_name == "LENGTH" and dialect == Dialects.MSSQL:
                        expected = "LEN"
                    self.assertIn(expected, str(query).upper())
