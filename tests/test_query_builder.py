import unittest

from objection import Model, QueryBuilder, RelationType


class TestQueryBuilder(unittest.TestCase):
    def test_select_from(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("id", "name")
        self.assertEqual(str(query), "SELECT id, name FROM users")

    def test_where(self):
        class User(Model):
            tableName = "users"

        query = User.query().select("name").where("age", ">", 21)
        self.assertEqual(str(query), "SELECT name FROM users WHERE age > 21")

    def test_join_related(self):
        class Person(Model):
            tableName = "persons"

        class Animal(Model):
            tableName = "animals"
            relationMappings = {
                "owner": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": Person,
                    "join": {"from": "animals.ownerId", "to": "persons.id"},
                }
            }

        query = (
            Animal.query()
            .select("animals.name", "persons.name")
            .leftOuterJoinRelated("owner")
        )
        self.assertEqual(
            str(query),
            "SELECT animals.name, persons.name FROM animals LEFT OUTER JOIN persons ON animals.ownerId = persons.id",
        )

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


if __name__ == "__main__":
    unittest.main()
