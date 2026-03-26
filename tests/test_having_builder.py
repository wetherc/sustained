import unittest

from sustained.builders.having_builder import HavingClauseBuilder
from sustained.model import Model


class TestHavingClauseBuilder(unittest.TestCase):
    def test_basic_having(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").groupBy("name").having("name", "=", "Test")
        self.assertEqual(
            str(query), "SELECT name FROM users GROUP BY name HAVING name = 'Test'"
        )

    def test_and_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .groupBy("name", "age")
            .having("name", "=", "Test")
            .andHaving("age", ">", 20)
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users GROUP BY name, age HAVING name = 'Test' AND age > 20",
        )

    def test_or_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .groupBy("name", "age")
            .having("name", "=", "Test")
            .orHaving("age", ">", 20)
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users GROUP BY name, age HAVING name = 'Test' OR age > 20",
        )

    def test_grouped_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .groupBy("name", "age")
            .having(lambda q: q.having("age", ">", 20).orHaving("name", "=", "Test"))
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users GROUP BY name, age HAVING (age > 20 OR name = 'Test')",
        )

    def test_having_in(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .groupBy("age")
            .havingIn("age", [20, "foo'bar", 30])
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users GROUP BY age HAVING age IN (20, 'foo''bar', 30)",
        )

    def test_having_not_in(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .groupBy("age")
            .havingNotIn("age", [20, "foo'bar", 30])
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users GROUP BY age HAVING age NOT IN (20, 'foo''bar', 30)",
        )

    def test_having_starts_with_conjunction_raises_error(self):
        class User(Model):
            tableName = "users"

        with self.assertRaisesRegex(
            RuntimeError, "Cannot start a having clause with 'and'."
        ):
            User.query().andHaving("age", ">", 10)

        with self.assertRaisesRegex(
            RuntimeError, "Cannot start a having clause with 'or'."
        ):
            User.query().orHaving("age", ">", 10)

    def test_having_invalid_arguments_raises_error(self):
        class User(Model):
            tableName = "users"

        with self.assertRaises(ValueError):
            User.query().having("age", ">")  # Missing value

        with self.assertRaises(ValueError):
            User.query().having("age")  # Missing operator and value

    def test_having_explicit_none_operator_raises_error(self):
        class User(Model):
            tableName = "users"

        with self.assertRaisesRegex(
            ValueError, "Operator must be provided for non-callable having clause."
        ):
            # This simulates calling _add_internal with op=None and val provided
            # The __getattr__ method should prevent this from happening normally
            # but testing the _add_internal's guard is important.
            having_builder_instance = HavingClauseBuilder(User)
            having_builder_instance._add_internal("", "column_name", None, "value")

    def test_conditional_builder_unrecognized_method_raises_error(self):
        class User(Model):
            tableName = "users"

        having_builder_instance = HavingClauseBuilder(User)
        with self.assertRaisesRegex(
            AttributeError,
            "'HavingClauseBuilder' object has no attribute 'nonExistentConditionalMethod'",
        ):
            having_builder_instance.nonExistentConditionalMethod("col", "=", 1)

    def test_empty_having_builder_str(self):
        class User(Model):
            tableName = "users"

        having_builder_instance = HavingClauseBuilder(User)
        self.assertEqual(str(having_builder_instance), "")


if __name__ == "__main__":
    unittest.main()
