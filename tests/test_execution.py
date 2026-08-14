"""
End-to-end execution tests against an in-memory SQLite database. The
default dialect uses qmark placeholders, which matches sqlite3.
"""

import sqlite3
import unittest

from sustained import Model, RelationType


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


if __name__ == "__main__":
    unittest.main()
