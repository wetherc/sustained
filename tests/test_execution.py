"""
End-to-end execution tests against an in-memory SQLite database. The
default dialect uses qmark placeholders, which matches sqlite3.
"""

import sqlite3
import unittest

from sustained import Model, RelationType
from sustained.execution import set_statement_listener


class ExecOwner(Model):
    tableName = "owners"
    relationMappings = {
        "pets": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "ExecPet",
            "join": {"from": "owners.id", "to": "pets.owner_id"},
        }
    }


class ExecPet(Model):
    tableName = "pets"
    relationMappings = {
        "owner": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": ExecOwner,
            "join": {"from": "pets.owner_id", "to": "owners.id"},
        }
    }


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.execute(
            "CREATE TABLE pets (id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT)"
        )
        ExecOwner.bind(self.conn)
        ExecPet.bind(self.conn)

    def tearDown(self):
        ExecOwner.unbind()
        ExecPet.unbind()
        self.conn.close()

    def _seed(self):
        ExecOwner.query().insert(
            [
                {"id": 1, "name": "Ada"},
                {"id": 2, "name": "Grace"},
                {"id": 3, "name": "Alan"},
            ]
        ).run()
        ExecPet.query().insert(
            [
                {"id": 1, "owner_id": 1, "name": "Rex"},
                {"id": 2, "owner_id": 1, "name": "Mia"},
                {"id": 3, "owner_id": 2, "name": "Sam"},
            ]
        ).run()

    def test_insert_returns_rowcount(self):
        count = ExecOwner.query().insert([{"id": 1, "name": "Ada"}]).run()
        self.assertEqual(count, 1)

    def test_select_hydrates_models(self):
        self._seed()
        owners = ExecOwner.query().orderBy("id").run()
        self.assertEqual(len(owners), 3)
        self.assertIsInstance(owners[0], ExecOwner)
        self.assertEqual(owners[0].name, "Ada")

    def test_where_filters_with_parameters(self):
        self._seed()
        owners = ExecOwner.query().where("name", "=", "Grace").run()
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0].id, 2)

    def test_first_returns_one_or_none(self):
        self._seed()
        owner = ExecOwner.query().where("name", "=", "Ada").first()
        self.assertEqual(owner.id, 1)
        missing = ExecOwner.query().where("name", "=", "Nobody").first()
        self.assertIsNone(missing)

    def test_update_and_delete_report_rowcount(self):
        self._seed()
        updated = ExecOwner.query().update({"name": "Ada L"}).where("id", "=", 1).run()
        self.assertEqual(updated, 1)
        self.assertEqual(ExecOwner.query().where("id", "=", 1).first().name, "Ada L")
        deleted = ExecPet.query().delete().where("owner_id", "=", 1).run()
        self.assertEqual(deleted, 2)

    def test_eager_load_has_many(self):
        self._seed()
        owners = ExecOwner.query().orderBy("id").withGraphFetched("pets").run()
        self.assertEqual([p.name for p in owners[0].pets], ["Rex", "Mia"])
        self.assertEqual(len(owners[1].pets), 1)
        self.assertEqual(owners[2].pets, [])

    def test_eager_load_belongs_to_one(self):
        self._seed()
        pets = ExecPet.query().orderBy("id").withGraphFetched("owner").run()
        self.assertEqual(pets[0].owner.name, "Ada")
        self.assertEqual(pets[2].owner.name, "Grace")

    def test_unknown_relation_raises_immediately(self):
        with self.assertRaises(ValueError):
            ExecOwner.query().withGraphFetched("nope")

    def test_run_without_connection_raises(self):
        ExecOwner.unbind()
        with self.assertRaises(RuntimeError):
            ExecOwner.query().run()

    def test_explicit_connection_overrides_binding(self):
        self._seed()
        other = sqlite3.connect(":memory:")
        other.execute("CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT)")
        owners = ExecOwner.query().run(other)
        self.assertEqual(owners, [])
        other.close()


class TestEagerLoadEdgeCases(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.execute(
            "CREATE TABLE pets (id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT)"
        )
        ExecOwner.bind(self.conn)
        ExecPet.bind(self.conn)

    def tearDown(self):
        ExecOwner.unbind()
        ExecPet.unbind()
        self.conn.close()

    def test_eager_load_with_no_parents(self):
        owners = ExecOwner.query().withGraphFetched("pets").run()
        self.assertEqual(owners, [])

    def test_eager_load_with_null_keys(self):
        ExecPet.query().insert([{"id": 1, "owner_id": None, "name": "Stray"}]).run()
        pets = ExecPet.query().withGraphFetched("owner").run()
        self.assertIsNone(pets[0].owner)

    def test_eager_load_requires_parent_key_column(self):
        ExecOwner.query().insert([{"id": 1, "name": "Ada"}]).run()
        query = ExecOwner.query().select("name").withGraphFetched("pets")
        with self.assertRaises(ValueError):
            query.run()

    def test_eager_load_through_relation(self):
        from sustained import RelationType, create_model

        Tag = create_model("ExecTag", "tags")
        Tagged = create_model(
            "ExecTagged",
            "owners",
            mappings={
                "tags": {
                    "relation": RelationType.ManyToManyRelation,
                    "modelClass": Tag,
                    "join": {
                        "from": "owners.id",
                        "through": {
                            "from": {"table": "owners_tags", "key": "ownerId"},
                            "to": {"table": "owners_tags", "key": "tagId"},
                        },
                        "to": "tags.id",
                    },
                }
            },
        )
        self.conn.executescript("""
            CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE owners_tags (ownerId INTEGER, tagId INTEGER);
            INSERT INTO owners (id, name) VALUES (1, 'Ada'), (2, 'Bob');
            INSERT INTO tags (id, label) VALUES (10, 'vip'), (11, 'new');
            INSERT INTO owners_tags VALUES (1, 10), (1, 11);
            """)
        Tagged.bind(self.conn)
        try:
            owners = Tagged.query().orderBy("id").withGraphFetched("tags").run()
            self.assertEqual([t.label for t in owners[0].tags], ["vip", "new"])
            self.assertEqual(owners[1].tags, [])
            self.assertNotIn("sustained_parent_key", owners[0].tags[0].__dict__)
        finally:
            Tagged.unbind()

    def test_eager_load_unqualified_join_ref_raises(self):
        from sustained import RelationType, create_model

        Pet2 = create_model("ExecPet2", "pets")
        Bad = create_model(
            "ExecBadOwner",
            "owners",
            mappings={
                "pets": {
                    "relation": RelationType.HasManyRelation,
                    "modelClass": Pet2,
                    "join": {"from": "id", "to": "pets.owner_id"},
                }
            },
        )
        Bad.bind(self.conn)
        self.conn.execute("INSERT INTO owners (id, name) VALUES (1, 'Ada')")
        with self.assertRaises(ValueError):
            Bad.query().withGraphFetched("pets").run()
        Bad.unbind()


class NestOwner(Model):
    tableName = "owners"
    relationMappings = {
        "pets": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "NestPet",
            "join": {"from": "owners.id", "to": "pets.owner_id"},
        }
    }


class NestPet(Model):
    tableName = "pets"
    relationMappings = {
        "toys": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "NestToy",
            "join": {"from": "pets.id", "to": "toys.pet_id"},
        },
        "owner": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": NestOwner,
            "join": {"from": "pets.owner_id", "to": "owners.id"},
        },
    }


class NestToy(Model):
    tableName = "toys"
    relationMappings = {}


class TestNestedEagerLoad(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE owners (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE pets (id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT);
            CREATE TABLE toys (id INTEGER PRIMARY KEY, pet_id INTEGER, name TEXT);
            INSERT INTO owners VALUES (1, 'Ada'), (2, 'Grace'), (3, 'Alan');
            INSERT INTO pets VALUES (1, 1, 'Rex'), (2, 1, 'Mia'), (3, 2, 'Sam');
            INSERT INTO toys VALUES (1, 1, 'Ball'), (2, 1, 'Rope'), (3, 3, 'Bone');
            """)
        for model in (NestOwner, NestPet, NestToy):
            model.bind(self.conn)
        self.statements = []
        set_statement_listener(lambda sql, params, seconds: self.statements.append(sql))

    def tearDown(self):
        set_statement_listener(None)
        for model in (NestOwner, NestPet, NestToy):
            model.unbind()
        self.conn.close()

    def test_dotted_path_loads_two_levels(self):
        owners = NestOwner.query().orderBy("id").withGraphFetched("pets.toys").run()
        self.assertEqual([p.name for p in owners[0].pets], ["Rex", "Mia"])
        self.assertEqual([t.name for t in owners[0].pets[0].toys], ["Ball", "Rope"])
        self.assertEqual(owners[0].pets[1].toys, [])
        self.assertEqual(owners[1].pets[0].toys[0].name, "Bone")

    def test_one_query_per_relation_per_level(self):
        NestOwner.query().withGraphFetched("pets.toys").run()
        self.assertEqual(len(self.statements), 3)
        self.assertIn("FROM toys", self.statements[2])

    def test_shared_prefix_loads_the_prefix_once(self):
        owners = (
            NestOwner.query()
            .orderBy("id")
            .withGraphFetched("pets.toys", "pets.owner")
            .run()
        )
        self.assertEqual(len(self.statements), 4)
        self.assertEqual(owners[0].pets[0].owner.name, "Ada")
        self.assertEqual(owners[0].pets[0].toys[0].name, "Ball")

    def test_deeper_level_skipped_when_nothing_attached(self):
        toys = NestToy.query().where("id", "=", 99).run()
        self.assertEqual(toys, [])
        self.statements.clear()
        owners = (
            NestOwner.query().where("id", "=", 3).withGraphFetched("pets.toys").run()
        )
        self.assertEqual(owners[0].pets, [])
        self.assertEqual(len(self.statements), 2)

    def test_to_one_middle_level_flattens(self):
        pets = NestPet.query().orderBy("id").withGraphFetched("owner.pets").run()
        self.assertEqual([p.name for p in pets[0].owner.pets], ["Rex", "Mia"])

    def test_unknown_segment_names_model_and_path(self):
        with self.assertRaises(ValueError) as caught:
            NestOwner.query().withGraphFetched("pets.wheels")
        message = str(caught.exception)
        self.assertIn("'wheels'", message)
        self.assertIn("NestPet", message)
        self.assertIn("pets.wheels", message)

    def test_unknown_first_segment_keeps_the_plain_message(self):
        with self.assertRaises(ValueError) as caught:
            NestOwner.query().withGraphFetched("wheels")
        self.assertEqual(
            str(caught.exception),
            "Relation 'wheels' not found in model 'NestOwner'",
        )


if __name__ == "__main__":
    unittest.main()
