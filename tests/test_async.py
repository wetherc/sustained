"""
Async execution tests using the DbApiAsyncAdapter over SQLite.
"""

import sqlite3
import unittest

from sustained import Model, RelationType
from sustained.aio import (
    DbApiAsyncAdapter,
    async_transaction,
    convert_format_to_numbered,
)
from sustained.execution import set_statement_listener
from sustained.schema import Integer, String


class AioOwner(Model):
    tableName = "aio_owners"
    tableColumns = {"id": Integer(primary_key=True), "name": String(50)}
    relationMappings = {
        "pets": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "AioPet",
            "join": {"from": "aio_owners.id", "to": "aio_pets.owner_id"},
        }
    }


class AioPet(Model):
    tableName = "aio_pets"
    tableColumns = {
        "id": Integer(primary_key=True),
        "owner_id": Integer(),
        "name": String(50),
    }
    relationMappings = {
        "toys": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "AioToy",
            "join": {"from": "aio_pets.id", "to": "aio_toys.pet_id"},
        },
        "owner": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": AioOwner,
            "join": {"from": "aio_pets.owner_id", "to": "aio_owners.id"},
        },
    }


class AioToy(Model):
    tableName = "aio_toys"
    tableColumns = {
        "id": Integer(primary_key=True),
        "pet_id": Integer(),
        "name": String(50),
    }


class AioTag(Model):
    tableName = "aio_tags"
    tableColumns = {"id": Integer(primary_key=True), "label": String(50)}


class AioTagged(Model):
    tableName = "aio_owners"
    tableColumns = {"id": Integer(primary_key=True), "name": String(50)}
    relationMappings = {
        "tags": {
            "relation": RelationType.ManyToManyRelation,
            "modelClass": AioTag,
            "join": {
                "from": "aio_owners.id",
                "through": {
                    "from": {"table": "aio_owner_tags", "key": "owner_id"},
                    "to": {"table": "aio_owner_tags", "key": "tag_id"},
                },
                "to": "aio_tags.id",
            },
        }
    }


class RecordingAdapter(DbApiAsyncAdapter):
    """Keeps the SQL text of every statement, for transaction-control tests."""

    def __init__(self, connection):
        super().__init__(connection)
        self.statements = []

    async def execute(self, sql, params):
        self.statements.append(sql)
        return await super().execute(sql, params)


class AsyncTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        AioOwner.create_table(self.conn)
        AioPet.create_table(self.conn)
        self.adapter = DbApiAsyncAdapter(self.conn)
        AioOwner.bind_async(self.adapter)
        AioPet.bind_async(self.adapter)

    def tearDown(self):
        AioOwner.unbind_async()
        AioPet.unbind_async()
        self.conn.close()


class TestAsyncExecution(AsyncTestCase):
    async def test_insert_and_select(self):
        count = (
            await AioOwner.query()
            .insert([{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}])
            .arun()
        )
        self.assertEqual(count, 2)
        owners = await AioOwner.query().orderBy("id").arun()
        self.assertEqual([o.name for o in owners], ["Ada", "Grace"])
        self.assertIsInstance(owners[0], AioOwner)

    async def test_afirst(self):
        await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
        owner = await AioOwner.query().where("name", "=", "Ada").afirst()
        self.assertEqual(owner.id, 1)
        missing = await AioOwner.query().where("name", "=", "X").afirst()
        self.assertIsNone(missing)

    async def test_ato_dicts(self):
        await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
        rows = await AioOwner.query().ato_dicts()
        self.assertEqual(rows, [{"id": 1, "name": "Ada"}])

    async def test_update_and_delete(self):
        await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
        updated = (
            await AioOwner.query().update({"name": "Ada L"}).where("id", "=", 1).arun()
        )
        self.assertEqual(updated, 1)
        deleted = await AioOwner.query().delete().where("id", "=", 1).arun()
        self.assertEqual(deleted, 1)

    async def test_returning(self):
        rows = (
            await AioOwner.query().insert({"id": 7, "name": "R"}).returning("id").arun()
        )
        self.assertEqual(rows, [{"id": 7}])

    async def test_eager_loading(self):
        await AioOwner.query().insert(
            [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Bob"}]
        ).arun()
        await AioPet.query().insert(
            [
                {"id": 1, "owner_id": 1, "name": "Rex"},
                {"id": 2, "owner_id": 1, "name": "Mia"},
            ]
        ).arun()
        owners = await AioOwner.query().orderBy("id").withGraphFetched("pets").arun()
        self.assertEqual([p.name for p in owners[0].pets], ["Rex", "Mia"])
        self.assertEqual(owners[1].pets, [])

    async def test_missing_adapter_raises(self):
        AioOwner.unbind_async()
        with self.assertRaises(RuntimeError):
            await AioOwner.query().arun()


class TestAsyncTransactions(AsyncTestCase):
    async def test_commit_on_success(self):
        async with AioOwner.async_transaction():
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
            await AioOwner.query().insert({"id": 2, "name": "Grace"}).arun()
        owners = await AioOwner.query().arun()
        self.assertEqual(len(owners), 2)

    async def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            async with AioOwner.async_transaction():
                await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
                raise RuntimeError("boom")
        owners = await AioOwner.query().arun()
        self.assertEqual(owners, [])

    async def test_nested_block_commits_with_the_outer_one(self):
        async with AioOwner.async_transaction():
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
            async with AioOwner.async_transaction():
                await AioOwner.query().insert({"id": 2, "name": "Grace"}).arun()
        owners = await AioOwner.query().orderBy("id").arun()
        self.assertEqual([o.name for o in owners], ["Ada", "Grace"])

    async def test_inner_failure_rolls_back_only_the_inner_block(self):
        async with AioOwner.async_transaction():
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
            with self.assertRaises(RuntimeError):
                async with AioOwner.async_transaction():
                    await AioOwner.query().insert({"id": 2, "name": "Grace"}).arun()
                    raise RuntimeError("boom")
        owners = await AioOwner.query().arun()
        self.assertEqual([o.name for o in owners], ["Ada"])

    async def test_outer_failure_rolls_back_a_released_inner_block(self):
        with self.assertRaises(RuntimeError):
            async with AioOwner.async_transaction():
                async with AioOwner.async_transaction():
                    await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
                raise RuntimeError("boom")
        owners = await AioOwner.query().arun()
        self.assertEqual(owners, [])

    async def test_savepoint_names_follow_the_nesting_depth(self):
        spy = RecordingAdapter(self.conn)
        async with async_transaction(spy):
            async with async_transaction(spy):
                async with async_transaction(spy):
                    pass
        self.assertEqual(
            spy.statements,
            [
                "BEGIN",
                "SAVEPOINT sustained_sp_1",
                "SAVEPOINT sustained_sp_2",
                "RELEASE SAVEPOINT sustained_sp_2",
                "RELEASE SAVEPOINT sustained_sp_1",
                "COMMIT",
            ],
        )

    async def test_depth_resets_so_a_later_block_reuses_the_first_name(self):
        spy = RecordingAdapter(self.conn)
        async with async_transaction(spy):
            async with async_transaction(spy):
                pass
            async with async_transaction(spy):
                pass
        self.assertEqual(spy.statements.count("SAVEPOINT sustained_sp_1"), 2)

    async def test_pinned_adapter_used_inside_block(self):
        AioOwner.unbind_async()
        async with async_transaction(self.adapter):
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
        AioOwner.bind_async(self.adapter)
        owners = await AioOwner.query().arun()
        self.assertEqual(len(owners), 1)


class TestAsyncNestedEagerLoad(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.executescript("""
            CREATE TABLE aio_owners (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE aio_pets (id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT);
            CREATE TABLE aio_toys (id INTEGER PRIMARY KEY, pet_id INTEGER, name TEXT);
            CREATE TABLE aio_tags (id INTEGER PRIMARY KEY, label TEXT);
            CREATE TABLE aio_owner_tags (owner_id INTEGER, tag_id INTEGER);
            INSERT INTO aio_owners VALUES (1, 'Ada'), (2, 'Grace'), (3, 'Alan');
            INSERT INTO aio_pets VALUES (1, 1, 'Rex'), (2, 1, 'Mia'), (3, 2, 'Sam');
            INSERT INTO aio_toys VALUES (1, 1, 'Ball'), (2, 1, 'Rope'), (3, 3, 'Bone');
            INSERT INTO aio_tags VALUES (10, 'vip'), (11, 'new');
            INSERT INTO aio_owner_tags VALUES (1, 10), (1, 11);
            """)
        self.adapter = DbApiAsyncAdapter(self.conn)
        self.models = (AioOwner, AioPet, AioToy, AioTag, AioTagged)
        for model in self.models:
            model.bind_async(self.adapter)
        self.statements = []
        set_statement_listener(lambda sql, params, seconds: self.statements.append(sql))

    def tearDown(self):
        set_statement_listener(None)
        for model in self.models:
            model.unbind_async()
        self.conn.close()

    async def test_dotted_path_loads_two_levels(self):
        owners = (
            await AioOwner.query().orderBy("id").withGraphFetched("pets.toys").arun()
        )
        self.assertEqual([p.name for p in owners[0].pets], ["Rex", "Mia"])
        self.assertEqual([t.name for t in owners[0].pets[0].toys], ["Ball", "Rope"])
        self.assertEqual(owners[0].pets[1].toys, [])
        self.assertEqual(owners[1].pets[0].toys[0].name, "Bone")

    async def test_one_query_per_relation_per_level(self):
        await AioOwner.query().withGraphFetched("pets.toys").arun()
        self.assertEqual(len(self.statements), 3)
        self.assertIn("FROM aio_toys", self.statements[2])

    async def test_shared_prefix_loads_the_prefix_once(self):
        owners = (
            await AioOwner.query()
            .orderBy("id")
            .withGraphFetched("pets.toys", "pets.owner")
            .arun()
        )
        self.assertEqual(len(self.statements), 4)
        self.assertEqual(owners[0].pets[0].owner.name, "Ada")

    async def test_through_relation_loads(self):
        owners = await AioTagged.query().orderBy("id").withGraphFetched("tags").arun()
        self.assertEqual([t.label for t in owners[0].tags], ["vip", "new"])
        self.assertEqual(owners[1].tags, [])
        self.assertNotIn("sustained_parent_key", owners[0].tags[0].__dict__)

    async def test_no_parent_keys_attaches_empty(self):
        await AioPet.query().insert({"id": 4, "owner_id": None, "name": "Stray"}).arun()
        pets = await AioPet.query().where("id", "=", 4).withGraphFetched("owner").arun()
        self.assertIsNone(pets[0].owner)


class TestPlaceholderConversion(unittest.TestCase):
    def test_sequential_numbering(self):
        self.assertEqual(
            convert_format_to_numbered("SELECT * FROM t WHERE a = %s AND b = %s"),
            "SELECT * FROM t WHERE a = $1 AND b = $2",
        )

    def test_no_placeholders(self):
        self.assertEqual(convert_format_to_numbered("SELECT 1"), "SELECT 1")


if __name__ == "__main__":
    unittest.main()
