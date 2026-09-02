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
            def compile_function(self, func, *, ctx=None):
                return f"KEYWORD_FUNCTION({ctx is not None})"

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

    def test_an_override_that_names_the_context_is_given_it_by_name(self):
        from sustained.expressions import Func

        _, _, keyword_style = self.compilers()
        compiler = keyword_style(Dialects.DEFAULT)
        func = Func("upper", ["name"])
        self.assertEqual(
            compiler.compile_function(func, object()), "KEYWORD_FUNCTION(True)"
        )
        self.assertEqual(compiler.compile_function(func), "KEYWORD_FUNCTION(False)")

    def test_a_method_the_inspect_module_cannot_read_is_left_alone(self):
        import math

        from sustained.compilers.base import _context_mode

        # math.hypot has no signature the inspect module can read.
        self.assertEqual(_context_mode(math.hypot, 1), "positional")

    def test_a_static_override_keeps_its_call_shape(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import WindowExpression

        class StaticStyle(Compiler):
            @staticmethod
            def compile_window(window):
                return f"STATIC_WINDOW({window.function_name})"

        window = WindowExpression("row_number", "r")
        compiler = StaticStyle(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_window(window, None), "STATIC_WINDOW(row_number)"
        )
        self.assertEqual(
            StaticStyle.compile_window(window), "STATIC_WINDOW(row_number)"
        )

    def test_a_static_override_that_takes_the_context_is_left_alone(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import WindowExpression

        class StaticContext(Compiler):
            @staticmethod
            def compile_window(window, ctx=None):
                return f"STATIC_CTX({ctx is not None})"

        window = WindowExpression("row_number", "r")
        compiler = StaticContext(Dialects.DEFAULT)
        self.assertEqual(compiler.compile_window(window, object()), "STATIC_CTX(True)")
        self.assertEqual(StaticContext.compile_window(window), "STATIC_CTX(False)")

    def test_a_static_override_that_names_the_context(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import WindowExpression

        class StaticKeyword(Compiler):
            @staticmethod
            def compile_window(window, *, ctx=None):
                return f"STATIC_KEYWORD({ctx is not None})"

        window = WindowExpression("row_number", "r")
        compiler = StaticKeyword(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_window(window, object()), "STATIC_KEYWORD(True)"
        )
        self.assertEqual(StaticKeyword.compile_window(window), "STATIC_KEYWORD(False)")

    def test_a_class_override_keeps_its_call_shape(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import WindowExpression

        class ClassStyle(Compiler):
            @classmethod
            def compile_window(cls, window):
                return f"CLASS_WINDOW({cls.__name__}, {window.function_name})"

        window = WindowExpression("row_number", "r")
        compiler = ClassStyle(Dialects.DEFAULT)
        self.assertEqual(
            compiler.compile_window(window, None),
            "CLASS_WINDOW(ClassStyle, row_number)",
        )
        self.assertEqual(
            ClassStyle.compile_window(window), "CLASS_WINDOW(ClassStyle, row_number)"
        )

    def test_a_subclass_of_a_subclass_overrides_again(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import Func

        class Parent(Compiler):
            def compile_function(self, func):
                return f"PARENT({func.function_name})"

        class Child(Parent):
            def compile_function(self, func):
                return f"CHILD({func.function_name})"

        func = Func("upper", ["name"])
        self.assertEqual(
            Parent(Dialects.DEFAULT).compile_function(func, None), "PARENT(upper)"
        )
        self.assertEqual(
            Child(Dialects.DEFAULT).compile_function(func, None), "CHILD(upper)"
        )

    def test_a_subclass_that_does_not_override_keeps_the_parent(self):
        from sustained.compilers.base import Compiler
        from sustained.expressions import Func

        class Parent(Compiler):
            def compile_function(self, func):
                return f"PARENT({func.function_name})"

        class Child(Parent):
            pass

        self.assertEqual(
            Child(Dialects.DEFAULT).compile_function(Func("upper", ["name"]), None),
            "PARENT(upper)",
        )

    def test_an_attribute_that_is_not_a_function_is_left_alone(self):
        from sustained.compilers.base import Compiler

        class NotAMethod(Compiler):
            compile_window = "not a method"

        self.assertEqual(NotAMethod.compile_window, "not a method")

    def test_an_old_override_renders_a_select_list(self):
        from sustained.builders.select_clause_builder import SelectClauseBuilder
        from sustained.expressions import Func, WindowExpression

        old_style, _, _ = self.compilers()
        builder = SelectClauseBuilder(old_style(Dialects.DEFAULT))
        builder.select(Func("upper", ["name"]), WindowExpression("row_number", "r"))
        self.assertEqual(str(builder), "OLD_FUNCTION(upper), OLD_WINDOW(row_number)")
