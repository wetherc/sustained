"""
Tests for the parts of execution that guard against silent damage:
transaction ownership across threads, savepoint cleanup, pool checkout
inside a transaction, and per-parent eager-load lists.
"""

import sqlite3
import threading
import unittest

from sustained import Model
from sustained.dialects import Dialects
from sustained.exceptions import GuardBlocked
from sustained.execution import attach_eager_load, plan_eager_load, transaction
from sustained.pool import ConnectionPool
from sustained.types import RelationType


class SafeOwner(Model):
    tableName = "owners"
    relationMappings = {
        "pets": {
            "relation": RelationType.HasManyRelation,
            "modelClass": "SafePet",
            "join": {"from": "owners.id", "to": "pets.owner_id"},
        }
    }


class SafePet(Model):
    tableName = "pets"


class TestSharedListPerParent(unittest.TestCase):
    def test_parents_with_the_same_key_get_their_own_list(self):
        parents = [SafeOwner(id=1), SafeOwner(id=1)]
        plan = plan_eager_load(SafeOwner, parents, "pets")
        attach_eager_load(plan, parents, [SafePet(id=7, owner_id=1)])

        self.assertEqual(len(parents[0].pets), 1)
        parents[0].pets.append(SafePet(id=8, owner_id=1))
        self.assertEqual(len(parents[1].pets), 1)

    def test_parents_with_no_match_get_their_own_list(self):
        parents = [SafeOwner(id=1), SafeOwner(id=2)]
        plan = plan_eager_load(SafeOwner, parents, "pets")
        attach_eager_load(plan, parents, [])

        parents[0].pets.append(SafePet(id=8, owner_id=1))
        self.assertEqual(parents[1].pets, [])


class TestTransactionThreadOwnership(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE t (id INTEGER)")

    def tearDown(self):
        self.conn.close()

    def test_second_thread_is_refused(self):
        opened = threading.Event()
        finish = threading.Event()
        errors = []

        def hold():
            with transaction(self.conn):
                opened.set()
                finish.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        opened.wait(5)
        try:
            with self.assertRaises(RuntimeError) as caught:
                with transaction(self.conn):
                    pass
            self.assertIn("Another thread", str(caught.exception))
        finally:
            finish.set()
            holder.join(5)
        self.assertEqual(errors, [])


class RecordingCursor:
    """A cursor that remembers every statement it was asked to run."""

    def __init__(self, fail_on=None):
        self.statements = []
        self._fail_on = fail_on
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if self._fail_on is not None and sql.startswith(self._fail_on):
            raise RuntimeError("rollback failed")

    def close(self):
        pass


class RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class TestSavepointCleanup(unittest.TestCase):
    def test_rolled_back_savepoint_is_released(self):
        cursor = RecordingCursor()
        conn = RecordingConnection(cursor)
        with transaction(conn, Dialects.POSTGRES):
            with self.assertRaises(ValueError):
                with transaction(conn, Dialects.POSTGRES):
                    raise ValueError("inner")

        self.assertIn("ROLLBACK TO SAVEPOINT sustained_sp_1", cursor.statements)
        self.assertIn("RELEASE SAVEPOINT sustained_sp_1", cursor.statements)

    def test_repeated_nesting_reuses_the_savepoint_name(self):
        cursor = RecordingCursor()
        conn = RecordingConnection(cursor)
        with transaction(conn, Dialects.POSTGRES):
            for _ in range(2):
                with self.assertRaises(ValueError):
                    with transaction(conn, Dialects.POSTGRES):
                        raise ValueError("inner")

        releases = [s for s in cursor.statements if s.startswith("RELEASE")]
        self.assertEqual(len(releases), 2)

    def test_failing_rollback_keeps_the_original_error(self):
        cursor = RecordingCursor(fail_on="ROLLBACK TO")
        conn = RecordingConnection(cursor)
        with transaction(conn, Dialects.POSTGRES):
            with self.assertRaises(ValueError) as caught:
                with transaction(conn, Dialects.POSTGRES):
                    raise ValueError("inner")
        self.assertEqual(str(caught.exception), "inner")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


class PoolModel(Model):
    tableName = "widgets"


class TestPoolInsideTransaction(unittest.TestCase):
    def test_query_given_the_pool_runs_on_the_pinned_connection(self):
        made = []

        def factory():
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
            made.append(conn)
            return conn

        pool = ConnectionPool(factory, max_size=1)
        try:
            with self.assertRaises(ValueError):
                with transaction(pool):
                    PoolModel.query().insert({"id": 1, "name": "a"}).run(pool)
                    raise ValueError("undo it")
            with transaction(pool):
                rows = PoolModel.query().run(pool)
            self.assertEqual(rows, [])
            self.assertEqual(len(made), 1)
        finally:
            pool.close()


class TestGuardBlockedWithoutVerdicts(unittest.TestCase):
    def test_empty_verdict_list_still_builds_the_error(self):
        error = GuardBlocked([])
        self.assertIn("A guard blocked this run", str(error))
        self.assertEqual(error.verdicts, [])


if __name__ == "__main__":
    unittest.main()
