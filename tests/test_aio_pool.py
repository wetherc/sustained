"""
Tests for AsyncConnectionPool: checkout, reuse, the max_size ceiling, the
refusals, and running real async queries through a bound pool.
"""

import asyncio
import sqlite3
import threading
import time
import unittest

from sustained import Model
from sustained.aio import AsyncAdapter, DbApiAsyncAdapter
from sustained.aio_pool import AsyncConnectionPool
from sustained.pool import PoolTimeout
from sustained.schema import Integer, String


class Widget(Model):
    tableName = "pool_widgets"
    tableColumns = {"id": Integer(primary_key=True), "name": String(40)}
    columns = ("id", "name")


class CountingAdapter(AsyncAdapter):
    """An adapter that records its calls and can hold a statement open."""

    def __init__(self, name, delay=0.0):
        self.name = name
        self.delay = delay
        self.statements = []
        self.rollbacks = 0
        self.closed = False
        self.running = 0
        self.most_at_once = 0

    async def fetch(self, sql, params):
        await self._work(sql)
        return ["one"], [(1,)]

    async def execute(self, sql, params):
        await self._work(sql)
        return 1

    async def executemany(self, sql, seq_of_params):
        await self._work(sql)
        return len(seq_of_params)

    async def _work(self, sql):
        self.statements.append(sql)
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.running -= 1

    async def commit(self):
        self.statements.append("COMMIT")

    async def rollback(self):
        self.rollbacks += 1

    async def close(self):
        self.closed = True


class RefusingRollbackAdapter(CountingAdapter):
    async def rollback(self):
        raise RuntimeError("this connection is gone")


class SlowCursor:
    """A cursor whose statement takes time, to show a lock holding."""

    def __init__(self, connection):
        self._connection = connection
        self.description = None
        self.rowcount = 1

    def execute(self, sql, params=()):
        self._connection.enter()
        time.sleep(self._connection.delay)
        self._connection.leave()

    def executemany(self, sql, seq):
        self.execute(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class SlowConnection:
    """Counts how many of its statements run at the same time."""

    def __init__(self, delay):
        self.delay = delay
        self.running = 0
        self.most_at_once = 0
        self._lock = threading.Lock()

    def enter(self):
        with self._lock:
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)

    def leave(self):
        with self._lock:
            self.running -= 1

    def cursor(self):
        return SlowCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def counting_factory(delay=0.0, adapter_class=CountingAdapter):
    made = []

    async def factory():
        adapter = adapter_class(f"a{len(made)}", delay)
        made.append(adapter)
        return adapter

    return factory, made


class TestPoolCheckout(unittest.IsolatedAsyncioTestCase):
    async def test_an_adapter_is_opened_lazily(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory)
        self.assertEqual(pool.size, 0)
        async with pool.scope():
            self.assertEqual(pool.size, 1)
        self.assertEqual(len(made), 1)

    async def test_a_released_adapter_is_reused(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory)
        async with pool.scope() as first:
            pass
        async with pool.scope() as second:
            self.assertIs(first, second)
        self.assertEqual(pool.size, 1)

    async def test_release_rolls_back_before_reuse(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory)
        async with pool.scope() as adapter:
            await adapter.execute("INSERT INTO t VALUES (1)", ())
        self.assertEqual(adapter.rollbacks, 1)

    async def test_max_size_caps_the_pool(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory, max_size=2, timeout=0.05)
        first = await pool.acquire()
        second = await pool.acquire()
        self.assertEqual(pool.size, 2)
        with self.assertRaises(PoolTimeout):
            await pool.acquire()
        await pool.release(first)
        await pool.release(second)

    async def test_a_waiting_task_gets_the_released_adapter(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory, max_size=1, timeout=5.0)
        held = await pool.acquire()

        async def give_back():
            await asyncio.sleep(0.01)
            await pool.release(held)

        asyncio.create_task(give_back())
        waited = await pool.acquire()
        self.assertIs(waited, held)
        self.assertEqual(pool.size, 1)
        await pool.release(waited)

    async def test_max_size_must_be_positive(self):
        factory, _ = counting_factory()
        with self.assertRaisesRegex(ValueError, "at least 1"):
            AsyncConnectionPool(factory, max_size=0)


class TestPoolRefusals(unittest.IsolatedAsyncioTestCase):
    async def test_releasing_a_foreign_adapter_raises(self):
        factory, _ = counting_factory()
        pool = AsyncConnectionPool(factory)
        with self.assertRaisesRegex(ValueError, "not checked out"):
            await pool.release(CountingAdapter("outsider"))

    async def test_a_double_release_raises(self):
        factory, _ = counting_factory()
        pool = AsyncConnectionPool(factory)
        adapter = await pool.acquire()
        await pool.release(adapter)
        with self.assertRaises(ValueError):
            await pool.release(adapter)

    async def test_the_pool_runs_no_statement_itself(self):
        factory, _ = counting_factory()
        pool = AsyncConnectionPool(factory)
        for call in (
            pool.fetch("SELECT 1", ()),
            pool.execute("SELECT 1", ()),
            pool.executemany("SELECT 1", []),
            pool.commit(),
            pool.rollback(),
        ):
            with self.assertRaisesRegex(RuntimeError, "pool.scope"):
                await call

    async def test_an_adapter_that_cannot_roll_back_is_dropped(self):
        factory, made = counting_factory(adapter_class=RefusingRollbackAdapter)
        pool = AsyncConnectionPool(factory, max_size=1)
        adapter = await pool.acquire()
        await pool.release(adapter)
        self.assertTrue(adapter.closed)
        self.assertEqual(pool.size, 0)
        # The slot reopened, so the next checkout gets a new adapter.
        async with pool.scope() as fresh:
            self.assertIsNot(fresh, adapter)


class TestPoolClose(unittest.IsolatedAsyncioTestCase):
    async def test_close_closes_the_idle_adapters(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory)
        async with pool.scope():
            pass
        await pool.close()
        self.assertTrue(made[0].closed)
        self.assertEqual(pool.size, 0)

    async def test_a_closed_pool_refuses_a_checkout(self):
        factory, _ = counting_factory()
        pool = AsyncConnectionPool(factory)
        await pool.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await pool.acquire()

    async def test_an_adapter_released_after_close_is_closed(self):
        factory, made = counting_factory()
        pool = AsyncConnectionPool(factory)
        adapter = await pool.acquire()
        await pool.close()
        await pool.release(adapter)
        self.assertTrue(adapter.closed)
        self.assertEqual(pool.size, 0)


class TestPoolRunsQueries(unittest.IsolatedAsyncioTestCase):
    """A bound pool is what arun() and async_transaction() run on."""

    def setUp(self):
        self.connections = []

        async def factory():
            connection = sqlite3.connect(self.path, check_same_thread=False)
            self.connections.append(connection)
            return DbApiAsyncAdapter(connection)

        import tempfile

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = f"{self.dir.name}/pool.db"
        setup = sqlite3.connect(self.path)
        setup.execute("CREATE TABLE pool_widgets (id INTEGER PRIMARY KEY, name TEXT)")
        setup.commit()
        setup.close()
        self.pool = AsyncConnectionPool(factory, max_size=2)
        Widget.bind_async(self.pool)
        self.addCleanup(Widget.unbind_async)

    async def asyncTearDown(self):
        await self.pool.close()
        for connection in self.connections:
            try:
                connection.close()
            except Exception:
                pass

    async def test_a_write_through_the_pool_commits(self):
        await Widget.query().insert({"id": 1, "name": "hinge"}).arun()
        rows = await Widget.query().select("name").arun()
        self.assertEqual([r.name for r in rows], ["hinge"])

    async def test_a_transaction_holds_one_adapter(self):
        from sustained.aio import async_transaction

        async with async_transaction(self.pool) as adapter:
            self.assertIsNot(adapter, self.pool)
            await Widget.query().insert({"id": 2, "name": "latch"}).arun()
        rows = await Widget.query().select("name").arun()
        self.assertEqual([r.name for r in rows], ["latch"])

    async def test_a_rolled_back_transaction_keeps_nothing(self):
        from sustained.aio import async_transaction

        with self.assertRaises(RuntimeError):
            async with async_transaction(self.pool):
                await Widget.query().insert({"id": 3, "name": "bolt"}).arun()
                raise RuntimeError("boom")
        rows = await Widget.query().select("name").arun()
        self.assertEqual(rows, [])

    async def test_the_pool_is_given_back_after_every_call(self):
        for index in range(5):
            await Widget.query().insert({"id": index + 10, "name": "x"}).arun()
        self.assertLessEqual(self.pool.size, 2)


class TestPoolRunsInParallel(unittest.IsolatedAsyncioTestCase):
    """
    One adapter serializes, which is the reason the pool exists. Two
    adapters run two statements at the same time.
    """

    async def test_one_dbapi_adapter_runs_one_statement_at_a_time(self):
        connection = SlowConnection(0.05)
        adapter = DbApiAsyncAdapter(connection)
        started = asyncio.get_running_loop().time()
        await asyncio.gather(
            adapter.execute("A", ()),
            adapter.execute("B", ()),
        )
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(connection.most_at_once, 1)
        self.assertGreaterEqual(elapsed, 0.1)

    async def test_two_pooled_adapters_run_at_the_same_time(self):
        factory, made = counting_factory(delay=0.05)
        pool = AsyncConnectionPool(factory, max_size=2)

        async def one():
            async with pool.scope() as adapter:
                await adapter.execute("SELECT 1", ())

        started = asyncio.get_running_loop().time()
        await asyncio.gather(one(), one())
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(len(made), 2)
        self.assertLess(elapsed, 0.09)

    async def test_a_single_adapter_pool_serializes(self):
        factory, made = counting_factory(delay=0.05)
        pool = AsyncConnectionPool(factory, max_size=1, timeout=5.0)

        async def one():
            async with pool.scope() as adapter:
                await adapter.execute("SELECT 1", ())

        started = asyncio.get_running_loop().time()
        await asyncio.gather(one(), one())
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(len(made), 1)
        self.assertGreaterEqual(elapsed, 0.1)


if __name__ == "__main__":
    unittest.main()
