"""
A write that raises outside a transaction rolls its partial work back, so
the next write's commit does not keep it.
"""

import sqlite3
import unittest

from sustained import Model
from sustained.aio import AsyncAdapter, DbApiAsyncAdapter, async_transaction
from sustained.execution import transaction
from sustained.schema import Integer, String


class FwrItem(Model):
    tableName = "fwr_items"
    tableColumns = {"id": Integer(primary_key=True), "name": String(20)}


# Row 3 repeats id 1, so executemany fails after rows 1 and 2 ran.
ROWS = [
    {"id": 1, "name": "a"},
    {"id": 2, "name": "b"},
    {"id": 1, "name": "dup"},
]


def items(conn):
    return conn.execute("SELECT id FROM fwr_items ORDER BY id").fetchall()


class RefusingRollback:
    def rollback(self):
        raise RuntimeError("rollback refused")


class RefusingAsyncRollback(AsyncAdapter):
    async def rollback(self):
        raise RuntimeError("rollback refused")


class TestSyncFailedWrite(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        FwrItem.create_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_failed_batch_leaves_nothing_for_the_next_commit(self):
        with self.assertRaises(sqlite3.IntegrityError):
            FwrItem.query().insert(ROWS).run(self.conn)
        self.assertFalse(self.conn.in_transaction)
        FwrItem.query().insert({"id": 9, "name": "z"}).run(self.conn)
        self.assertEqual(items(self.conn), [(9,)])

    def test_a_failure_inside_a_block_leaves_the_rollback_to_the_block(self):
        with transaction(self.conn):
            FwrItem.query().insert({"id": 5, "name": "e"}).run(self.conn)
            with self.assertRaises(sqlite3.IntegrityError):
                FwrItem.query().insert(ROWS + ROWS).run(self.conn)
            self.assertTrue(self.conn.in_transaction)
        self.assertIn((5,), items(self.conn))

    def test_a_failed_select_does_not_roll_back(self):
        self.conn.execute("INSERT INTO fwr_items VALUES (7, 'g')")
        with self.assertRaises(sqlite3.OperationalError):
            FwrItem.query().where("missing", "=", 1).run(self.conn)
        self.assertTrue(self.conn.in_transaction)

    def test_a_refused_rollback_is_dropped(self):
        from sustained.execution import rollback_quietly

        rollback_quietly(RefusingRollback())


class TestAsyncFailedWrite(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        FwrItem.create_table(self.conn)
        self.adapter = DbApiAsyncAdapter(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_a_failed_batch_leaves_nothing_for_the_next_commit(self):
        with self.assertRaises(sqlite3.IntegrityError):
            await FwrItem.query().insert(ROWS).arun(self.adapter)
        self.assertFalse(self.conn.in_transaction)
        await FwrItem.query().insert({"id": 9, "name": "z"}).arun(self.adapter)
        self.assertEqual(items(self.conn), [(9,)])

    async def test_a_failure_inside_a_block_leaves_the_rollback_to_the_block(self):
        async with async_transaction(self.adapter):
            await FwrItem.query().insert({"id": 5, "name": "e"}).arun()
            with self.assertRaises(sqlite3.IntegrityError):
                await FwrItem.query().insert(ROWS + ROWS).arun()
            self.assertTrue(self.conn.in_transaction)
        self.assertIn((5,), items(self.conn))

    async def test_a_refused_rollback_keeps_the_write_error(self):
        with self.assertRaises(NotImplementedError):
            await FwrItem.query().where("id", "=", 1).delete().arun(
                RefusingAsyncRollback()
            )


if __name__ == "__main__":
    unittest.main()
