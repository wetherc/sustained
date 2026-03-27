import unittest
from typing import Dict

from src.sustained import Model, RelationType
from src.sustained.builders.join_builder import (
    JoinClauseBuilder,  # For direct testing where needed
)
from src.sustained.types import RelationMapping


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
                },
                "unknown_model": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": "NonExistentModel",  # For testing model resolution
                    "join": {"from": "animals.unknownId", "to": "unknown.id"},
                },
            }

        class Car(Model):
            tableName = "cars"
            relationMappings = {
                "engine": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": "EngineModelWithNoneTableName",
                    "join": {"from": "cars.engineId", "to": "engines.id"},
                }
            }

        # Define a model for testing missing tableName assertion
        class EngineModelWithNoneTableName(Model):
            tableName = None  # Explicitly set to None for the test

        self.Person = Person
        self.Animal = Animal
        self.Movie = Movie
        self.Car = Car
        self.EngineModelWithNoneTableName = EngineModelWithNoneTableName

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

    def test_raw_join_with_using(self):
        query = self.Animal.query().join("persons", using=["ownerId", "personId"])
        self.assertEqual(
            str(query),
            "SELECT * FROM animals JOIN persons USING (ownerId, personId)",
        )

    def test_raw_join_with_subquery_in_on(self):
        subquery = self.Person.query().select("id").where("age", ">", 40)
        query = self.Animal.query().join("persons", "persons.id", "=", subquery)
        self.assertEqual(
            str(query),
            "SELECT * FROM animals JOIN persons ON persons.id = (SELECT id FROM persons WHERE age > 40)",
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

    def test_join_related_missing_relation_raises_error(self):
        with self.assertRaisesRegex(
            ValueError, "Relation 'non_existent' not found in model 'Animal'"
        ):
            self.Animal.query().joinRelated("non_existent")

    def test_add_basic_join_with_none_table_name_raises_assertion_error(self):
        # Temporarily modify relationMappings for this test
        original_mappings = self.Car.relationMappings
        temp_mappings: Dict[str, RelationMapping] = {
            "engine": {
                "relation": RelationType.BelongsToOneRelation,
                "modelClass": self.EngineModelWithNoneTableName,
                "join": {"from": "cars.engineId", "to": "engine.id"},
            }
        }
        self.Car.relationMappings = temp_mappings

        with self.assertRaisesRegex(
            AssertionError, "Model used in a relation must have a tableName"
        ):
            self.Car.query().joinRelated("engine")

        # Restore original mappings
        self.Car.relationMappings = original_mappings

    def test_join_related_alias_to_col_mismatch(self):
        class User(Model):
            tableName = "users"
            relationMappings = {
                "profile": {
                    "relation": RelationType.HasOneRelation,
                    "modelClass": "UserProfile",
                    "join": {
                        "from": "users.profileId",
                        "to": "external_profiles.id",
                    },  # to_col points to external_profiles
                }
            }

        class UserProfile(Model):
            tableName = "user_profiles"  # The related model's actual table name

        # Temporarily set UserProfile in the current module for _resolve_model_class
        import sys

        sys.modules[__name__].UserProfile = UserProfile

        query = User.query().joinRelated("profile", alias="p")
        # Expectation: The ON clause should not be aliased because 'external_profiles' != 'user_profiles'
        # It should remain 'users.profileId = external_profiles.id'
        self.assertEqual(
            str(query),
            "SELECT * FROM users JOIN user_profiles AS p ON users.profileId = external_profiles.id",
        )
        del sys.modules[__name__].UserProfile

    def test_through_join_alias_to_col_mismatch(self):
        class Tag(Model):
            tableName = "tags"

        class Post(Model):
            tableName = "posts"
            relationMappings = {
                "tags": {
                    "relation": RelationType.ManyToManyRelation,
                    "modelClass": Tag,
                    "join": {
                        "from": "posts.id",
                        "through": {
                            "from": {"table": "post_tags", "key": "postId"},
                            "to": {"table": "post_tags", "key": "tagId"},
                        },
                        "to": "another_tags_table.id",  # to_col points to another_tags_table
                    },
                }
            }

        # Temporarily set Tag in the current module for _resolve_model_class
        import sys

        sys.modules[__name__].Tag = Tag

        query = Post.query().joinRelated("tags", alias="t")
        # Expectation: The ON clause should not be aliased because 'another_tags_table' != 'tags'
        # It should remain 'post_tags.tagId = another_tags_table.id'
        self.assertEqual(
            str(query),
            "SELECT * FROM posts INNER JOIN post_tags ON posts.id = post_tags.postId JOIN tags AS t ON post_tags.tagId = another_tags_table.id",
        )
        del sys.modules[__name__].Tag


if __name__ == "__main__":
    unittest.main()
