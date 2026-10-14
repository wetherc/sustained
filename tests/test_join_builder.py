import sqlite3
import unittest
from typing import Dict

from sustained import Model, RelationType
from sustained.builders.join_builder import (  # For direct testing where needed
    JoinClauseBuilder,
    OnClauseBuilder,
)
from sustained.types import RelationMapping


class TestJoinBuilder(unittest.TestCase):
    def setUp(self):
        class Show(Model):
            tableName = "shows"

        class Performer(Model):
            tableName = "performers"
            relationMappings = {
                "shows": {
                    "relation": RelationType.ManyToManyRelation,
                    "modelClass": Show,
                    "join": {
                        "from": "performers.id",
                        "through": {
                            "from": {"table": "lineups", "key": "performer_id"},
                            "to": {"table": "lineups", "key": "show_id"},
                        },
                        "to": "shows.id",
                    },
                }
            }

        class Recording(Model):
            tableName = "recordings"
            relationMappings = {
                "performer": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": Performer,
                    "join": {"from": "recordings.performer_id", "to": "performers.id"},
                },
                "unknown_model": {
                    "relation": RelationType.BelongsToOneRelation,
                    "modelClass": "NonExistentModel",  # For testing model resolution
                    "join": {"from": "recordings.unknownId", "to": "unknown.id"},
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

        self.Performer = Performer
        self.Recording = Recording
        self.Show = Show
        self.Car = Car
        self.EngineModelWithNoneTableName = EngineModelWithNoneTableName

    def test_inner_join_related(self):
        query = self.Recording.query().innerJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings INNER JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_left_join_related(self):
        query = self.Recording.query().leftJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings LEFT JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_right_join_related(self):
        query = self.Recording.query().rightJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings RIGHT JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_right_outer_join_related(self):
        query = self.Recording.query().rightOuterJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings RIGHT OUTER JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_full_outer_join_related(self):
        query = self.Recording.query().fullOuterJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings FULL OUTER JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_cross_join_related(self):
        query = self.Recording.query().crossJoinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings CROSS JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_join_related_with_alias(self):
        query = self.Recording.query().innerJoinRelated("performer", alias="p")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings INNER JOIN performers AS p ON recordings.performer_id = p.id",
        )

    def test_through_join(self):
        query = self.Performer.query().innerJoinRelated("shows")
        self.assertEqual(
            str(query),
            "SELECT * FROM performers INNER JOIN lineups ON performers.id = lineups.performer_id INNER JOIN shows ON lineups.show_id = shows.id",
        )

    def test_left_join_related_through(self):
        query = self.Performer.query().leftJoinRelated("shows")
        self.assertEqual(
            str(query),
            "SELECT * FROM performers LEFT JOIN lineups ON performers.id = lineups.performer_id LEFT JOIN shows ON lineups.show_id = shows.id",
        )

    def test_through_join_link_hop_follows_the_join_type(self):
        cases = {
            "leftOuterJoinRelated": ("LEFT OUTER JOIN", "LEFT OUTER JOIN"),
            "fullJoinRelated": ("LEFT JOIN", "FULL JOIN"),
            "fullOuterJoinRelated": ("LEFT OUTER JOIN", "FULL OUTER JOIN"),
            "rightJoinRelated": ("INNER JOIN", "RIGHT JOIN"),
            "joinRelated": ("INNER JOIN", "INNER JOIN"),
        }
        for method, (link_join, far_join) in cases.items():
            with self.subTest(method=method):
                query = getattr(self.Performer.query(), method)("shows")
                self.assertEqual(
                    str(query),
                    f"SELECT * FROM performers {link_join} lineups "
                    "ON performers.id = lineups.performer_id "
                    f"{far_join} shows ON lineups.show_id = shows.id",
                )

    def test_left_join_through_keeps_rows_with_no_link(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE performers (id INTEGER, name TEXT);"
            "CREATE TABLE shows (id INTEGER, title TEXT);"
            'CREATE TABLE lineups ("performer_id" INTEGER, "show_id" INTEGER);'
            "INSERT INTO performers VALUES (1, 'Ann'), (2, 'Bo');"
            "INSERT INTO shows VALUES (10, 'Heat');"
            "INSERT INTO lineups VALUES (1, 10);"
        )
        query = (
            self.Performer.query()
            .select("performers.name", "shows.title")
            .leftJoinRelated("shows")
            .orderBy("performers.name")
        )
        rows = conn.execute(str(query)).fetchall()
        self.assertEqual(rows, [("Ann", "Heat"), ("Bo", None)])
        conn.close()

    def test_through_join_with_alias(self):
        query = self.Performer.query().innerJoinRelated("shows", alias="m")
        self.assertEqual(
            str(query),
            "SELECT * FROM performers INNER JOIN lineups ON performers.id = lineups.performer_id INNER JOIN shows AS m ON lineups.show_id = m.id",
        )

    def test_repeated_through_join_aliases_the_link_table(self):
        query = (
            self.Performer.query()
            .innerJoinRelated("shows", alias="first")
            .leftJoinRelated("shows", alias="second")
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM performers "
            "INNER JOIN lineups ON performers.id = lineups.performer_id "
            "INNER JOIN shows AS first ON lineups.show_id = first.id "
            "LEFT JOIN lineups AS second_lineups "
            "ON performers.id = second_lineups.performer_id "
            "LEFT JOIN shows AS second ON second_lineups.show_id = second.id",
        )

    def test_repeated_through_join_without_alias_raises(self):
        query = self.Performer.query().innerJoinRelated("shows")
        with self.assertRaisesRegex(ValueError, "lineups"):
            query.innerJoinRelated("shows")

    def test_default_join_related(self):
        query = self.Recording.query().joinRelated("performer")
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_raw_join(self):
        query = self.Recording.query().join(
            "performers", "recordings.performer_id", "=", "performers.id"
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_raw_left_join(self):
        query = self.Recording.query().leftJoin(
            "performers", "recordings.performer_id", "=", "performers.id"
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings LEFT JOIN performers ON recordings.performer_id = performers.id",
        )

    def test_raw_join_rejects_an_unknown_operator(self):
        with self.assertRaises(ValueError):
            self.Recording.query().join(
                "performers",
                "recordings.performer_id",
                "= performers.id OR 1=1 --",
                "performers.id",
            )

    def test_raw_join_normalizes_the_operator(self):
        query = self.Recording.query().join(
            "performers", "recordings.performer_id", "not like", "performers.id"
        )
        self.assertIn("ON recordings.performer_id NOT LIKE performers.id", str(query))

    def test_raw_join_with_using(self):
        query = self.Recording.query().join(
            "performers", using=["performer_id", "tour_id"]
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings JOIN performers USING (performer_id, tour_id)",
        )

    def test_raw_join_with_subquery_in_on(self):
        subquery = self.Performer.query().select("id").where("age", ">", 40)
        query = self.Recording.query().join(
            "performers", "performers.id", "=", subquery
        )
        self.assertEqual(
            str(query),
            "SELECT * FROM recordings JOIN performers ON performers.id = (SELECT id FROM performers WHERE age > 40)",
        )

    def test_with_clause(self):
        class Order(Model):
            tableName = "orders"

        large_orders = Order.query().select("id").where("amount", ">", 1000)
        query = self.Recording.query().with_("large_orders", large_orders).select("*")
        self.assertEqual(
            str(query),
            "WITH large_orders AS (SELECT id FROM orders WHERE amount > 1000) SELECT * FROM recordings",
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
            ValueError, "Relation 'non_existent' not found in model 'Recording'"
        ):
            self.Recording.query().joinRelated("non_existent")

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

        with self.assertRaisesRegex(ValueError, "must define a tableName"):
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
            "SELECT * FROM posts INNER JOIN post_tags ON posts.id = post_tags.postId INNER JOIN tags AS t ON post_tags.tagId = another_tags_table.id",
        )
        del sys.modules[__name__].Tag


class TestJoinBuilderStr(unittest.TestCase):
    """str() renders the join clauses with the values inlined."""

    def setUp(self):
        class Ticket(Model):
            tableName = "tickets"

        self.Ticket = Ticket

    def test_str_of_the_join_builder(self):
        builder = JoinClauseBuilder(self.Ticket)
        builder.join("shows", "tickets.show_id", "=", "shows.id")
        self.assertEqual(str(builder), "JOIN shows ON tickets.show_id = shows.id")

    def test_str_of_the_on_clause_builder(self):
        sub = self.Ticket.query().select("show_id").where("a", "=", 1)
        on_builder = OnClauseBuilder()
        on_builder.on("tickets.show_id", "=", sub)
        self.assertEqual(
            str(on_builder),
            "tickets.show_id = (SELECT show_id FROM tickets WHERE a = 1)",
        )

    def test_on_clause_builder_without_a_condition_raises(self):
        with self.assertRaises(RuntimeError):
            str(OnClauseBuilder())


class SelfJoinCategory(Model):
    tableName = "categories"
    tableSchema = "shop"
    relationMappings = {
        "children": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "SelfJoinCategory",
            "join": {"from": "shop.categories.id", "to": "categories.parent_id"},
        },
        "related": {
            "relation": RelationType.ManyToManyRelation,
            "modelClass": "SelfJoinCategory",
            "join": {
                "from": "categories.id",
                "through": {
                    "from": {"table": "category_links", "key": "from_id"},
                    "to": {"table": "category_links", "key": "to_id"},
                },
                "to": "categories.id",
            },
        },
    }


class TestSelfRelationJoin(unittest.TestCase):
    def test_self_join_without_alias_raises(self):
        for relation in ("children", "related"):
            with self.subTest(relation=relation):
                query = SelfJoinCategory.query()
                with self.assertRaisesRegex(ValueError, "alias="):
                    query.joinRelated(relation)

    def test_self_join_with_alias_renders(self):
        query = SelfJoinCategory.query().leftJoinRelated("children", alias="kid")
        self.assertEqual(
            str(query),
            "SELECT * FROM shop.categories LEFT JOIN shop.categories AS kid "
            "ON shop.categories.id = kid.parent_id",
        )


if __name__ == "__main__":
    unittest.main()
