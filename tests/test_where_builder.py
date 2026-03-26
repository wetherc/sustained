import unittest

from src.sustained import Model, QueryBuilder, RelationType


class TestWhereBuilder(unittest.TestCase):
    def test_where(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").where("age", ">", 21)
        self.assertEqual(str(query), "SELECT name FROM users WHERE age > 21")

    def test_and_where(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .select("name")
            .where("age", ">", 21)
            .andWhere("name", "!=", "John Doe")
        )
        self.assertEqual(
            str(query), "SELECT name FROM users WHERE age > 21 AND name != 'John Doe'"
        )

    def test_or_where(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query().select("name").where("age", ">", 21).orWhere("age", "<", 10)
        )
        self.assertEqual(
            str(query), "SELECT name FROM users WHERE age > 21 OR age < 10"
        )

    def test_grouped_where(self):
        class User(Model):
            tableName = "users"

        query = (
            User.query()
            .where("status", "=", "active")
            .andWhere(
                lambda q: (q.where("age", ">", 21).orWhere("name", "=", "John Doe"))
            )
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE status = 'active' AND (age > 21 OR name = 'John Doe')",
        )

    def test_complex_grouped_where(self):
        class User(Model):
            tableName = "users"
            database = "my_db"
            tableSchema = "public"

        query = (
            User.query()
            .where("age", ">=", 18)
            .where(
                lambda q: (
                    q.where("name", "=", "John Doe").orWhere("name", "=", "Jane Doe")
                )
            )
            .andWhere("status", "=", "active")
        )

        expected_sql = "SELECT * FROM my_db.public.users WHERE age >= 18 AND (name = 'John Doe' OR name = 'Jane Doe') AND status = 'active'"
        self.assertEqual(str(query), expected_sql)

    def test_where_in(self):
        class User(Model):
            tableName = "users"

        query = User.query().whereIn("id", [1, 2, 3])
        self.assertEqual(str(query), "SELECT * FROM users WHERE id IN (1, 2, 3)")

    def test_where_not_in(self):
        class User(Model):
            tableName = "users"

        query = User.query().whereNotIn("id", [1, 2, 3])
        self.assertEqual(str(query), "SELECT * FROM users WHERE id NOT IN (1, 2, 3)")

    def test_where_in_with_subquery(self):
        class User(Model):
            tableName = "users"

        class BannedUser(Model):
            tableName = "banned_users"

        query = User.query().whereIn("id", BannedUser.query().select("user_id"))
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE id IN (SELECT user_id FROM banned_users)",
        )

    def test_where_between(self):
        class User(Model):
            tableName = "users"

        query = User.query().whereBetween("age", 18, 30)
        self.assertEqual(str(query), "SELECT * FROM users WHERE age BETWEEN 18 AND 30")

    def test_where_not_between(self):
        class User(Model):
            tableName = "users"

        query = User.query().whereNotBetween("age", 18, 30)
        self.assertEqual(
            str(query), "SELECT * FROM users WHERE age NOT BETWEEN 18 AND 30"
        )

    def test_where_exists(self):
        class User(Model):
            tableName = "users"

        class Post(Model):
            tableName = "posts"

        query = User.query().whereExists(
            Post.query()
            .select("id")
            .where("posts.user_id", "=", QueryBuilder.raw("users.id"))
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE EXISTS (SELECT id FROM posts WHERE posts.user_id = users.id)",
        )

    def test_where_not_exists(self):
        class User(Model):
            tableName = "users"

        class Post(Model):
            tableName = "posts"

        query = User.query().whereNotExists(
            Post.query()
            .select("id")
            .where("posts.user_id", "=", QueryBuilder.raw("users.id"))
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM users WHERE NOT EXISTS (SELECT id FROM posts WHERE posts.user_id = users.id)",
        )


if __name__ == "__main__":
    unittest.main()
