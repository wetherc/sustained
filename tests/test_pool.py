"""
Tests for the connection pool and its integration with run() and
transaction(), using a shared-cache in-memory SQLite database.
"""

import sqlite3
import threading
import unittest

from sustained import Model
from sustained.pool import ConnectionPool, PoolTimeout
from sustained.schema import Integer, String


class PoolUser(Model):
    tableName = "pool_users"
    tableColumns = {"id": Integer(primary_key=True), "name": String(50)}


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        self.uri = f"file:pool_{id(self)}?mode=memory&cache=shared"
        self.keeper = self._connect()
        PoolUser.create_table(self.keeper)
        self.pool = ConnectionPool(self._connect, max_size=3)
        PoolUser.bind(self.pool)

    def tearDown(self):
        PoolUser.unbind()
        self.pool.close()
        self.keeper.close()

    def _connect(self):
        return sqlite3.connect(self.uri, uri=True, check_same_thread=False)


class TestPoolBasics(unittest.TestCase):
    def test_lazy_creation_and_reuse(self):
        created = []

        def factory():
            created.append(True)
            return sqlite3.connect(":memory:")

        pool = ConnectionPool(factory, max_size=2)
        with pool.connection():
            pass
        with pool.connection():
            pass
        self.assertEqual(len(created), 1)
        pool.close()

    def test_max_size_and_timeout(self):
        pool = ConnectionPool(
            lambda: sqlite3.connect(":memory:"), max_size=1, timeout=0.05
        )
        conn = pool.acquire_raw()
        with self.assertRaises(PoolTimeout):
            pool.acquire_raw()
        pool.release(conn)
        pool.close()

    def test_failed_factory_frees_slot(self):
        attempts = []

        def factory():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("no database for you")
            return sqlite3.connect(":memory:")

        pool = ConnectionPool(factory, max_size=1, timeout=0.05)
        with self.assertRaises(RuntimeError):
            pool.acquire_raw()
        conn = pool.acquire_raw()
        self.assertIsNotNone(conn)
        pool.release(conn)
        pool.close()

    def test_closed_pool_rejects_acquire(self):
        pool = ConnectionPool(lambda: sqlite3.connect(":memory:"), max_size=1)
        pool.close()
        with self.assertRaises(RuntimeError):
            pool.acquire_raw()

    def test_invalid_max_size(self):
        with self.assertRaises(ValueError):
            ConnectionPool(lambda: None, max_size=0)


class FakeCursor:
    """Answers the pool's health probe and nothing else."""

    def execute(self, sql, parameters=()):
        self.sql = sql

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class FakeConnection:
    """A connection that records the hygiene calls the pool makes on it."""

    def __init__(self, autocommit=False, rollback_error=None, responds=False):
        self.autocommit = autocommit
        self.rollbacks = 0
        self.closed = False
        self._rollback_error = rollback_error
        self._responds = responds

    def cursor(self):
        if not self._responds:
            raise RuntimeError("this connection is gone")
        return FakeCursor()

    def commit(self):  # pragma: no cover - the pool never commits
        raise NotImplementedError

    def rollback(self):
        self.rollbacks += 1
        if self._rollback_error is not None:
            raise self._rollback_error

    def close(self):
        self.closed = True


class TestPoolHygiene(unittest.TestCase):
    def _pool(self, factory, **kwargs):
        pool = ConnectionPool(factory, **kwargs)
        self.addCleanup(pool.close)
        return pool

    def test_release_rolls_back_before_reuse(self):
        conn = FakeConnection()
        pool = self._pool(lambda: conn, max_size=1)
        pool.release(pool.acquire_raw())
        self.assertEqual(conn.rollbacks, 1)
        self.assertIs(pool.acquire_raw(), conn)

    def test_release_skips_rollback_in_autocommit(self):
        conn = FakeConnection(autocommit=True)
        pool = self._pool(lambda: conn, max_size=1)
        pool.release(pool.acquire_raw())
        self.assertEqual(conn.rollbacks, 0)

    def test_failed_rollback_discards_the_connection(self):
        made = [FakeConnection(rollback_error=RuntimeError("gone")), FakeConnection()]
        pool = self._pool(lambda: made.pop(0), max_size=1, timeout=0.05)
        first = pool.acquire_raw()
        pool.release(first)
        self.assertTrue(first.closed)
        self.assertEqual(pool.size, 0)
        second = pool.acquire_raw()
        self.assertIsNot(second, first)
        pool.release(second)

    def test_a_healthy_connection_that_refuses_rollback_is_kept(self):
        made = []

        def factory():
            conn = FakeConnection(
                rollback_error=RuntimeError("no transaction is active"),
                responds=True,
            )
            made.append(conn)
            return conn

        pool = self._pool(factory, max_size=1)
        first = pool.acquire_raw()
        pool.release(first)
        self.assertFalse(first.closed)
        self.assertIs(pool.acquire_raw(), first)
        pool.release(first)
        # The pool asked once, learned the driver refuses, and stopped.
        self.assertEqual(first.rollbacks, 1)
        self.assertEqual(len(made), 1)

    def test_double_release_is_refused(self):
        pool = self._pool(lambda: FakeConnection(), max_size=2)
        conn = pool.acquire_raw()
        pool.release(conn)
        with self.assertRaises(ValueError):
            pool.release(conn)
        self.assertEqual(pool.size, 1)

    def test_foreign_release_is_refused(self):
        pool = self._pool(lambda: FakeConnection(), max_size=1)
        with self.assertRaises(ValueError):
            pool.release(FakeConnection())
        self.assertEqual(pool.size, 0)

    def test_release_after_close_closes_the_connection(self):
        conn = FakeConnection()
        pool = ConnectionPool(lambda: conn, max_size=1)
        checked_out = pool.acquire_raw()
        pool.close()
        pool.release(checked_out)
        self.assertTrue(conn.closed)
        self.assertEqual(pool.size, 0)

    def test_close_frees_the_created_count(self):
        pool = ConnectionPool(lambda: FakeConnection(), max_size=2)
        pool.release(pool.acquire_raw())
        self.assertEqual(pool.size, 1)
        pool.close()
        self.assertEqual(pool.size, 0)


class TestPoolExecution(PoolTestCase):
    def test_run_checks_out_and_releases(self):
        PoolUser.query().insert([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]).run()
        rows = PoolUser.query().orderBy("id").run()
        self.assertEqual([m.id for m in rows], [1, 2])
        self.assertLessEqual(self.pool.size, 3)

    def test_transaction_pins_one_connection(self):
        with PoolUser.transaction():
            PoolUser.query().insert({"id": 1, "name": "a"}).run()
            self.assertEqual(len(PoolUser.query().run()), 1)
        self.assertEqual(len(PoolUser.query().run()), 1)

    def test_transaction_rollback_on_pool(self):
        with self.assertRaises(RuntimeError):
            with PoolUser.transaction():
                PoolUser.query().insert({"id": 1, "name": "a"}).run()
                raise RuntimeError("boom")
        self.assertEqual(PoolUser.query().run(), [])

    def test_nested_transaction_on_pool_uses_savepoint(self):
        with PoolUser.transaction():
            PoolUser.query().insert({"id": 1, "name": "outer"}).run()
            with self.assertRaises(RuntimeError):
                with PoolUser.transaction():
                    PoolUser.query().insert({"id": 2, "name": "inner"}).run()
                    raise RuntimeError("inner")
        self.assertEqual([m.id for m in PoolUser.query().run()], [1])

    def test_concurrent_reads(self):
        PoolUser.query().insert([{"id": i, "name": "x"} for i in range(1, 6)]).run()
        errors = []

        def worker():
            try:
                for _ in range(10):
                    PoolUser.query().where("id", "=", 1).run()
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(self.pool.size, 3)

    def test_explicit_pool_argument(self):
        PoolUser.unbind()
        PoolUser.query().insert({"id": 9, "name": "z"}).run(self.pool)
        rows = PoolUser.query().run(self.pool)
        self.assertEqual(rows[0].id, 9)
        PoolUser.bind(self.pool)


if __name__ == "__main__":
    unittest.main()
