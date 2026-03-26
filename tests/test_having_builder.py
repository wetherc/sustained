import unittest

from sustained.model import Model


class TestHavingClauseBuilder(unittest.TestCase):
    def test_basic_having(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").having("name", "=", "Test")
        self.assertEqual(str(query), "SELECT name FROM users HAVING name = 'Test'")

    def test_and_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .having("name", "=", "Test")
            .andHaving("age", ">", 20)
        )
        self.assertEqual(
            str(query), "SELECT name FROM users HAVING name = 'Test' AND age > 20"
        )

    def test_or_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .having("name", "=", "Test")
            .orHaving("age", ">", 20)
        )
        self.assertEqual(
            str(query), "SELECT name FROM users HAVING name = 'Test' OR age > 20"
        )

    def test_grouped_having(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .having(lambda q: q.having("age", ">", 20).orHaving("name", "=", "Test"))
        )
        self.assertEqual(
            str(query),
            "SELECT name FROM users HAVING (age > 20 OR name = 'Test')",
        )

    def test_having_in(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").havingIn("age", [20, 30])
        self.assertEqual(str(query), "SELECT name FROM users HAVING age IN (20, 30)")

    def test_having_not_in(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").havingNotIn("age", [20, 30])
        self.assertEqual(
            str(query), "SELECT name FROM users HAVING age NOT IN (20, 30)"
        )


if __name__ == "__main__":
    unittest.main()
