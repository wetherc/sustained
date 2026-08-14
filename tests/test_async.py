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

    async def test_nesting_rejected(self):
        async with AioOwner.async_transaction():
            with self.assertRaises(RuntimeError):
                async with AioOwner.async_transaction():
                    pass

    async def test_pinned_adapter_used_inside_block(self):
        AioOwner.unbind_async()
        async with async_transaction(self.adapter):
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun()
        AioOwner.bind_async(self.adapter)
        owners = await AioOwner.query().arun()
        self.assertEqual(len(owners), 1)


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
