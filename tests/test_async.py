"""
Async execution tests using the DbApiAsyncAdapter over SQLite.
"""

import sqlite3
import unittest

from sustained import Model, RelationType
from sustained.aio import (
    AsyncAdapter,
    AsyncpgAdapter,
    DbApiAsyncAdapter,
    async_transaction,
    convert_format_to_numbered,
)
from sustained.dialects import Dialects
from sustained.exceptions import DialectError
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
        # sqlite3 in legacy transaction control needs the BEGIN. The block
        # ends with the driver's commit(), which sends no statement.
        self.assertEqual(
            spy.statements,
            [
                "BEGIN",
                "SAVEPOINT sustained_sp_1",
                "SAVEPOINT sustained_sp_2",
                "RELEASE SAVEPOINT sustained_sp_2",
                "RELEASE SAVEPOINT sustained_sp_1",
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


class FakeAdapter(AsyncAdapter):
    """
    Records statements without running them, for dialect spelling tests.
    It has no transaction control of its own, so a block on it is opened
    and closed with statements.
    """

    def __init__(self):
        self.statements = []

    async def execute(self, sql, params):
        self.statements.append(sql)
        return 0


class TestAsyncTransactionDialects(unittest.IsolatedAsyncioTestCase):
    async def test_mssql_spells_save_transaction(self):
        fake = FakeAdapter()
        async with async_transaction(fake, Dialects.MSSQL):
            async with async_transaction(fake, Dialects.MSSQL):
                pass
        self.assertEqual(
            fake.statements,
            [
                "BEGIN TRANSACTION",
                "SAVE TRANSACTION sustained_sp_1",
                "COMMIT",
            ],
        )

    async def test_mssql_inner_failure_rolls_back_to_the_savepoint(self):
        fake = FakeAdapter()
        async with async_transaction(fake, Dialects.MSSQL):
            with self.assertRaises(RuntimeError):
                async with async_transaction(fake, Dialects.MSSQL):
                    raise RuntimeError("boom")
        self.assertIn("ROLLBACK TRANSACTION sustained_sp_1", fake.statements)

    async def test_duckdb_nesting_raises_before_any_statement(self):
        fake = FakeAdapter()
        async with async_transaction(fake, Dialects.DUCKDB):
            before = list(fake.statements)
            with self.assertRaises(DialectError):
                async with async_transaction(fake, Dialects.DUCKDB):
                    pass
            self.assertEqual(fake.statements, before)

    async def test_model_block_uses_the_model_dialect(self):
        fake = FakeAdapter()

        class MssqlOwner(Model):
            tableName = "aio_owners"
            tableColumns = {"id": Integer(primary_key=True)}

        MssqlOwner.set_dialect(Dialects.MSSQL)
        async with MssqlOwner.async_transaction(fake):
            async with MssqlOwner.async_transaction(fake):
                pass
        self.assertIn("SAVE TRANSACTION sustained_sp_1", fake.statements)


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

    async def test_unknown_segment_names_model_and_path(self):
        with self.assertRaises(ValueError) as caught:
            AioOwner.query().withGraphFetched("pets.wheels")
        message = str(caught.exception)
        self.assertIn("'wheels'", message)
        self.assertIn("AioPet", message)
        self.assertIn("pets.wheels", message)

    async def test_unknown_first_segment_keeps_the_plain_message(self):
        with self.assertRaises(ValueError) as caught:
            AioOwner.query().withGraphFetched("wheels")
        self.assertEqual(
            str(caught.exception),
            "Relation 'wheels' not found in model 'AioOwner'",
        )


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


class AutocommitAdapter(AsyncAdapter):
    """
    An adapter with no transaction control of its own, over a real sqlite3
    connection. It stands for asyncpg, which needs BEGIN and COMMIT as
    statements.
    """

    def __init__(self, connection):
        self._connection = connection
        self.statements = []

    async def fetch(self, sql, params):
        cursor = self._connection.cursor()
        cursor.execute(sql, params)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = list(cursor.fetchall())
        cursor.close()
        return columns, rows

    async def execute(self, sql, params):
        self.statements.append(sql)
        cursor = self._connection.cursor()
        cursor.execute(sql, params)
        count = cursor.rowcount
        cursor.close()
        return count

    async def commit(self):
        raise AssertionError("an autocommit adapter must not be committed")

    async def rollback(self):
        raise AssertionError("an autocommit adapter must not be rolled back")


class ImplicitBeginAdapter(DbApiAsyncAdapter):
    """
    A DB-API adapter whose driver opens the transaction itself, the way
    psycopg2 does. A second BEGIN raises.
    """

    def __init__(self, connection):
        super().__init__(connection)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, params):
        if sql.startswith("BEGIN"):
            raise RuntimeError("there is already a transaction in progress")
        self.statements.append(sql)
        return await super().execute(sql, params)

    async def begin_where_ddl_autocommits(self):
        return None

    async def commit(self):
        self.commits += 1
        await super().commit()

    async def rollback(self):
        self.rollbacks += 1
        await super().rollback()


class TestAsyncTransactionControl(AsyncTestCase):
    async def test_driver_with_its_own_begin_gets_no_begin_statement(self):
        adapter = ImplicitBeginAdapter(self.conn)
        async with async_transaction(adapter):
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun(adapter)
        self.assertEqual(adapter.commits, 1)
        self.assertEqual(adapter.rollbacks, 0)
        self.assertNotIn("COMMIT", adapter.statements)
        owners = await AioOwner.query().arun()
        self.assertEqual(len(owners), 1)

    async def test_driver_with_its_own_begin_rolls_back_through_the_driver(self):
        adapter = ImplicitBeginAdapter(self.conn)
        with self.assertRaises(RuntimeError):
            async with async_transaction(adapter):
                await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun(adapter)
                raise RuntimeError("boom")
        self.assertEqual(adapter.rollbacks, 1)
        self.assertEqual(adapter.commits, 0)
        self.assertNotIn("ROLLBACK", adapter.statements)
        owners = await AioOwner.query().arun()
        self.assertEqual(owners, [])

    async def test_autocommit_adapter_still_gets_begin_and_commit(self):
        adapter = AutocommitAdapter(self.conn)
        async with async_transaction(adapter):
            await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun(adapter)
        self.assertEqual(adapter.statements[0], "BEGIN")
        self.assertEqual(adapter.statements[-1], "COMMIT")

    async def test_autocommit_adapter_rolls_back_with_a_statement(self):
        adapter = AutocommitAdapter(self.conn)
        with self.assertRaises(RuntimeError):
            async with async_transaction(adapter):
                await AioOwner.query().insert({"id": 1, "name": "Ada"}).arun(adapter)
                raise RuntimeError("boom")
        self.assertEqual(adapter.statements[-1], "ROLLBACK")

    async def test_duckdb_dialect_drives_the_block_with_statements(self):
        adapter = ImplicitBeginAdapter(self.conn)
        # DuckDB autocommits every statement, so the dialect asks for SQL
        # control even though the adapter has its own. The BEGIN raises in
        # this stand-in, which proves the statement was sent.
        with self.assertRaises(RuntimeError):
            async with async_transaction(adapter, Dialects.DUCKDB):
                pass
        self.assertEqual(adapter.commits, 0)

    async def test_base_adapter_reports_no_transaction_control(self):
        self.assertFalse(AsyncAdapter().driver_transaction_control())
        await AsyncAdapter().begin_where_ddl_autocommits()

    async def test_dbapi_adapter_reports_transaction_control(self):
        self.assertTrue(self.adapter.driver_transaction_control())


class TestAsyncSavepointRelease(AsyncTestCase):
    async def test_inner_failure_releases_the_savepoint_it_rolled_back(self):
        spy = RecordingAdapter(self.conn)
        async with async_transaction(spy):
            with self.assertRaises(RuntimeError):
                async with async_transaction(spy):
                    raise RuntimeError("boom")
        self.assertEqual(
            spy.statements,
            [
                "BEGIN",
                "SAVEPOINT sustained_sp_1",
                "ROLLBACK TO SAVEPOINT sustained_sp_1",
                "RELEASE SAVEPOINT sustained_sp_1",
            ],
        )

    async def test_the_same_savepoint_name_can_be_used_again(self):
        async with AioOwner.async_transaction():
            for name in ("Ada", "Grace"):
                with self.assertRaises(RuntimeError):
                    async with AioOwner.async_transaction():
                        await AioOwner.query().insert({"id": 1, "name": name}).arun()
                        raise RuntimeError("boom")
            await AioOwner.query().insert({"id": 3, "name": "Hedy"}).arun()
        owners = await AioOwner.query().arun()
        self.assertEqual([o.name for o in owners], ["Hedy"])

    async def test_a_failing_rollback_keeps_the_original_error(self):
        class BrokenRollback(FakeAdapter):
            async def execute(self, sql, params):
                if sql.startswith("ROLLBACK TO"):
                    raise RuntimeError("connection lost")
                return await super().execute(sql, params)

        adapter = BrokenRollback()
        async with async_transaction(adapter):
            with self.assertRaises(ValueError) as caught:
                async with async_transaction(adapter):
                    raise ValueError("boom")
        self.assertEqual(str(caught.exception), "boom")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(str(caught.exception.__cause__), "connection lost")

    async def test_a_failing_release_keeps_the_original_error(self):
        class BrokenRelease(FakeAdapter):
            async def execute(self, sql, params):
                if sql.startswith("RELEASE"):
                    raise RuntimeError("connection lost")
                return await super().execute(sql, params)

        adapter = BrokenRelease()
        async with async_transaction(adapter):
            with self.assertRaises(ValueError) as caught:
                async with async_transaction(adapter):
                    raise ValueError("boom")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    async def test_a_dialect_without_a_release_sends_only_the_rollback(self):
        adapter = FakeAdapter()
        async with async_transaction(adapter, Dialects.MSSQL):
            with self.assertRaises(RuntimeError):
                async with async_transaction(adapter, Dialects.MSSQL):
                    raise RuntimeError("boom")
        self.assertEqual(
            adapter.statements,
            [
                "BEGIN TRANSACTION",
                "SAVE TRANSACTION sustained_sp_1",
                "ROLLBACK TRANSACTION sustained_sp_1",
                "COMMIT",
            ],
        )


class TestAsyncBatchInsertColumns(AsyncTestCase):
    async def test_an_extra_column_in_a_later_row_raises(self):
        with self.assertRaises(ValueError) as caught:
            AioOwner.query().insert(
                [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace", "extra": 1}]
            )
        self.assertIn("same columns", str(caught.exception))

    async def test_a_missing_column_in_a_later_row_raises(self):
        with self.assertRaises(ValueError):
            AioOwner.query().insert([{"id": 1, "name": "Ada"}, {"id": 2}])

    async def test_matching_rows_still_insert(self):
        count = await (
            AioOwner.query()
            .insert([{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}])
            .arun()
        )
        self.assertEqual(count, 2)


class FakeAsyncpgRecord:
    """A stand-in for an asyncpg record."""

    def __init__(self, mapping):
        self._mapping = mapping

    def keys(self):
        return list(self._mapping.keys())

    def __iter__(self):
        return iter(self._mapping.values())


class FakeAsyncpgConnection:
    """A stand-in for an asyncpg connection, with a scripted status string."""

    def __init__(self, status="INSERT 0 3", records=()):
        self.status = status
        self.records = list(records)
        self.calls = []
        self.closed = False

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.records

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self.status

    async def executemany(self, sql, args):
        self.calls.append((sql, args))
        return None

    async def close(self):
        self.closed = True


class TestAsyncpgAdapter(unittest.IsolatedAsyncioTestCase):
    async def test_execute_reads_the_count_out_of_the_status(self):
        adapter = AsyncpgAdapter(FakeAsyncpgConnection("INSERT 0 3"))
        self.assertEqual(await adapter.execute("INSERT INTO t VALUES (%s)", (1,)), 3)

    async def test_a_status_without_a_count_is_unknown(self):
        adapter = AsyncpgAdapter(FakeAsyncpgConnection("CREATE TABLE"))
        self.assertEqual(await adapter.execute("CREATE TABLE t (a int)", ()), -1)

    async def test_a_status_that_is_not_text_is_unknown(self):
        adapter = AsyncpgAdapter(FakeAsyncpgConnection(None))
        self.assertEqual(await adapter.execute("SELECT 1", ()), -1)

    async def test_executemany_is_unknown(self):
        connection = FakeAsyncpgConnection()
        adapter = AsyncpgAdapter(connection)
        count = await adapter.executemany("INSERT INTO t VALUES (%s)", [(1,), (2,)])
        self.assertEqual(count, -1)
        self.assertEqual(connection.calls[0][0], "INSERT INTO t VALUES ($1)")

    async def test_fetch_returns_columns_and_rows(self):
        records = [FakeAsyncpgRecord({"id": 1, "name": "Ada"})]
        adapter = AsyncpgAdapter(FakeAsyncpgConnection(records=records))
        columns, rows = await adapter.fetch("SELECT * FROM t WHERE a = %s", (1,))
        self.assertEqual(columns, ["id", "name"])
        self.assertEqual(rows, [(1, "Ada")])

    async def test_fetch_with_no_rows(self):
        adapter = AsyncpgAdapter(FakeAsyncpgConnection())
        self.assertEqual(await adapter.fetch("SELECT 1", ()), ([], []))

    async def test_commit_and_rollback_do_nothing(self):
        adapter = AsyncpgAdapter(FakeAsyncpgConnection())
        self.assertIsNone(await adapter.commit())
        self.assertIsNone(await adapter.rollback())

    async def test_close_closes_the_connection(self):
        connection = FakeAsyncpgConnection()
        await AsyncpgAdapter(connection).close()
        self.assertTrue(connection.closed)

    async def test_it_has_no_transaction_control_of_its_own(self):
        # asyncpg runs in autocommit, so a block on it needs BEGIN and
        # COMMIT as statements.
        adapter = AsyncpgAdapter(FakeAsyncpgConnection())
        self.assertFalse(adapter.driver_transaction_control())
