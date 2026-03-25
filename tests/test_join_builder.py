import unittest

from objection import Model, RelationType


class TestJoinBuilder(unittest.TestCase):
    def setUp(self):
        class Movie(Model):
            tableName = "movies"

        class Person(Model):
            tableName = "persons"
            relationMappings = {
                "movies": {
                    "relation": RelationType.ManyToManyRelation,
                    "modelClass": Movie,
                    "join": {
                        "from": "persons.id",
                        "through": {
                            "from": {"table": "persons_movies", "key": "personId"},
                            "to": {"table": "persons_movies", "key": "movieId"},
                        },
                        "to": "movies.id",
                    },
                }
            }

        class Animal(Model):
            tableName = "animals"
            relationMappings = {
                "owner": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": Person,
                    "join": {"from": "animals.ownerId", "to": "persons.id"},
                }
            }

        self.Person = Person
        self.Animal = Animal

    def test_inner_join_related(self):
        query = self.Animal.query().innerJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals INNER JOIN persons ON animals.ownerId = persons.id",
        )

    def test_left_join_related(self):
        query = self.Animal.query().leftJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals LEFT JOIN persons ON animals.ownerId = persons.id",
        )

    def test_right_join_related(self):
        query = self.Animal.query().rightJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals RIGHT JOIN persons ON animals.ownerId = persons.id",
        )

    def test_right_outer_join_related(self):
        query = self.Animal.query().rightOuterJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals RIGHT OUTER JOIN persons ON animals.ownerId = persons.id",
        )

    def test_full_outer_join_related(self):
        query = self.Animal.query().fullOuterJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals FULL OUTER JOIN persons ON animals.ownerId = persons.id",
        )

    def test_cross_join_related(self):
        query = self.Animal.query().crossJoinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals CROSS JOIN persons ON animals.ownerId = persons.id",
        )

    def test_join_related_with_alias(self):
        query = self.Animal.query().innerJoinRelated("owner", alias="p")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals INNER JOIN persons AS p ON animals.ownerId = p.id",
        )

    def test_through_join(self):
        query = self.Person.query().innerJoinRelated("movies")
        self.assertEqual(
            str(query),
            "SELECT * FROM persons INNER JOIN persons_movies ON persons.id = persons_movies.personId INNER JOIN movies ON persons_movies.movieId = movies.id",
        )

    def test_left_join_related_through(self):
        query = self.Person.query().leftJoinRelated("movies")
        self.assertEqual(
            str(query),
            "SELECT * FROM persons INNER JOIN persons_movies ON persons.id = persons_movies.personId LEFT JOIN movies ON persons_movies.movieId = movies.id",
        )

    def test_through_join_with_alias(self):
        query = self.Person.query().innerJoinRelated("movies", alias="m")
        self.assertEqual(
            str(query),
            "SELECT * FROM persons INNER JOIN persons_movies ON persons.id = persons_movies.personId INNER JOIN movies AS m ON persons_movies.movieId = m.id",
        )

    def test_default_join_related(self):
        query = self.Animal.query().joinRelated("owner")
        self.assertEqual(
            str(query),
            "SELECT * FROM animals JOIN persons ON animals.ownerId = persons.id",
        )

    def test_raw_join(self):
        query = self.Animal.query().join(
            "persons", "animals.ownerId", "=", "persons.id"
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM animals JOIN persons ON animals.ownerId = persons.id",
        )

    def test_raw_left_join(self):
        query = self.Animal.query().leftJoin(
            "persons", "animals.ownerId", "=", "persons.id"
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM animals LEFT JOIN persons ON animals.ownerId = persons.id",
        )

    def test_with_clause(self):
        class Order(Model):
            tableName = "orders"

        large_orders = Order.query().select("id").where("amount", ">", 1000)
        query = self.Animal.query().with_("large_orders", large_orders).select("*")
        self.assertEqual(
            str(query),
            "WITH large_orders AS (SELECT id FROM orders WHERE amount > 1000) SELECT * FROM animals",
        )

    def test_with_and_raw_join(self):
        class Order(Model):
            tableName = "orders"

        class Customer(Model):
            tableName = "customers"

        large_orders = (
            Order.query().select("id", "customer_id").where("amount", ">", 1000)
        )
        query = (
            Customer.query()
            .with_("large_orders", large_orders)
            .join("large_orders", "customers.id", "=", "large_orders.customer_id")
        )

        self.assertEqual(
            str(query),
            "WITH large_orders AS (SELECT id, customer_id FROM orders WHERE amount > 1000) SELECT * FROM customers JOIN large_orders ON customers.id = large_orders.customer_id",
        )


if __name__ == "__main__":
    unittest.main()
