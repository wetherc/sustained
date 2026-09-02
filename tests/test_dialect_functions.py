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


class TestOldCompilerOverrides(unittest.TestCase):
    """A compiler subclass written before the render context still works."""

    def compilers(self):
        from sustained.compilers.base import Compiler

        class OldStyle(Compiler):
            def compile_function(self, func):
                return f"OLD_FUNCTION({func.function_name})"

            def compile_function_call(self, func):
                return f"OLD_CALL({func.function_name})"

            def compile_window(self, window):
                return f"OLD_WINDOW({window.function_name})"

            def compile_window_call(self, window):
                return f"OLD_WINDOW_CALL({window.function_name})"

        class NewStyle(Compiler):
            def compile_function(self, func, ctx=None):
                return f"NEW_FUNCTION({ctx is not None})"

        class KeywordStyle(Compiler):
            def compile_function(self, func, **options):
                return f"KEYWORD_FUNCTION({func.function_name})"

        return OldStyle, NewStyle, KeywordStyle

    def test_an_old_override_takes_the_context_and_drops_it(self):
        from sustained.expressions import Func, WindowExpression

        old_style, _, _ = self.compilers()
        compiler = old_style(Dialects.DEFAULT)
        func = Func("upper", ["name"])
        window = WindowExpression("row_number", "r")
        self.assertEqual(compiler.compile_function(func, None), "OLD_FUNCTION(upper)")
        self.assertEqual(compiler.compile_function_call(func), "OLD_CALL(upper)")
        self.assertEqual(
            compiler.compile_window(window, None), "OLD_WINDOW(row_number)"
        )
        self.assertEqual(
            compiler.compile_window_call(window, None), "OLD_WINDOW_CALL(row_number)"
        )

    def test_a_new_override_keeps_the_context(self):
        from sustained.expressions import Func

        _, new_style, _ = self.compilers()
        compiler = new_style(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_function(Func("upper", ["name"]), object()),
            "NEW_FUNCTION(True)",
        )

    def test_an_override_that_takes_keywords_is_left_alone(self):
        from sustained.expressions import Func

        _, _, keyword_style = self.compilers()
        compiler = keyword_style(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_function(Func("upper", ["name"])),
            "KEYWORD_FUNCTION(upper)",
        )

    def test_an_old_override_renders_a_select_list(self):
        from sustained.builders.select_clause_builder import SelectClauseBuilder
        from sustained.expressions import Func, WindowExpression

        old_style, _, _ = self.compilers()
        builder = SelectClauseBuilder(old_style(Dialects.DEFAULT))
        builder.select(Func("upper", ["name"]), WindowExpression("row_number", "r"))
        self.assertEqual(str(builder), "OLD_FUNCTION(upper), OLD_WINDOW(row_number)")
