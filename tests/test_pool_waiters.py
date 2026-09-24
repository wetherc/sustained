"""
A pool wakes its waiters when a slot frees up, not only when a connection
comes back, and a release that races close() closes its connection.
"""

import asyncio
import threading
import time
import unittest

from sustained.aio import AsyncAdapter
from sustained.aio_pool import AsyncConnectionPool
from sustained.pool import ConnectionPool


class FakeConnection:
    """A DB-API connection that can refuse its rollback and every probe."""

    def __init__(self):
        self.broken = False
        self.closed = False
        self.on_rollback = None

    def rollback(self):
        if self.on_rollback is not None:
            self.on_rollback()
        if self.broken:
            raise RuntimeError("connection lost")

    def cursor(self):
        raise RuntimeError("connection lost")

    def close(self):
        self.closed = True


class TestSyncPoolWaiters(unittest.TestCase):
    def setUp(self):
        self.made = []

        def factory():
            conn = FakeConnection()
            self.made.append(conn)
            return conn

        self.pool = ConnectionPool(factory, max_size=1, timeout=5)

    def test_a_discard_wakes_a_waiter_to_open_a_new_connection(self):
        first = self.pool.acquire_raw()
        got = []
        waiter = threading.Thread(target=lambda: got.append(self.pool.acquire_raw()))
        started = time.monotonic()
        waiter.start()
        time.sleep(0.05)
        first.broken = True
        self.pool.release(first)
        waiter.join(2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(got), 1)
        self.assertIsNot(got[0], first)
        self.assertEqual(self.pool.size, 1)

    def test_close_wakes_a_waiter(self):
        self.pool.acquire_raw()
        errors = []

        def wait():
            try:
                self.pool.acquire_raw()
            except RuntimeError as error:
                errors.append(error)

        waiter = threading.Thread(target=wait)
        waiter.start()
        time.sleep(0.05)
        self.pool.close()
        waiter.join(2)
        self.assertEqual(len(errors), 1)
        self.assertIn("closed", str(errors[0]))

    def test_a_release_that_races_close_closes_its_connection(self):
        conn = self.pool.acquire_raw()
        conn.on_rollback = self.pool.close
        self.pool.release(conn)
        self.assertTrue(conn.closed)
        self.assertEqual(self.pool.size, 0)

    def test_a_failed_factory_frees_its_slot(self):
        def failing():
            raise RuntimeError("cannot connect")

        pool = ConnectionPool(failing, max_size=1, timeout=0.1)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                pool.acquire_raw()
        self.assertEqual(pool.size, 0)


class BrokenAdapter(AsyncAdapter):
    async def rollback(self):
        raise RuntimeError("connection lost")

    async def fetch(self, sql, params):
        raise RuntimeError("connection lost")

    async def close(self):
        pass


class QuietAdapter(AsyncAdapter):
    async def rollback(self):
        pass

    async def close(self):
        pass


class TestAsyncPoolWaiters(unittest.IsolatedAsyncioTestCase):
    async def test_a_discard_wakes_a_waiter_to_open_a_new_adapter(self):
        made = []

        async def factory():
            adapter = BrokenAdapter() if not made else QuietAdapter()
            made.append(adapter)
            return adapter

        pool = AsyncConnectionPool(factory, max_size=1, timeout=5)
        first = await pool.acquire()
        started = asyncio.get_running_loop().time()
        waiter = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        await pool.release(first)
        second = await asyncio.wait_for(waiter, 2)
        self.assertLess(asyncio.get_running_loop().time() - started, 2)
        self.assertIs(second, made[1])
        self.assertEqual(pool.size, 1)

    async def test_close_wakes_a_waiter(self):
        async def factory():
            return QuietAdapter()

        pool = AsyncConnectionPool(factory, max_size=1, timeout=5)
        await pool.acquire()
        waiter = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        await pool.close()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(waiter, 2)

    async def test_a_slow_connect_does_not_hold_up_a_release(self):
        gate = asyncio.Event()
        made = []

        async def factory():
            if made:
                await gate.wait()
            adapter = QuietAdapter()
            made.append(adapter)
            return adapter

        pool = AsyncConnectionPool(factory, max_size=2, timeout=5)
        first = await pool.acquire()
        slow = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        await asyncio.wait_for(pool.release(first), 1)
        self.assertIs(await pool.acquire(), first)
        gate.set()
        await slow
        self.assertEqual(pool.size, 2)

    async def test_a_failed_factory_frees_its_slot(self):
        async def factory():
            raise RuntimeError("cannot connect")

        pool = AsyncConnectionPool(factory, max_size=1, timeout=0.1)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await pool.acquire()
        self.assertEqual(pool.size, 0)

    async def test_a_woken_waiter_that_is_cancelled_passes_the_wake_on(self):
        async def factory():
            return QuietAdapter()

        pool = AsyncConnectionPool(factory, max_size=1, timeout=5)
        first = await pool.acquire()
        cancelled = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        patient = asyncio.ensure_future(pool.acquire())
        await asyncio.sleep(0.01)
        # The adapter comes back and wakes the first waiter, which is
        # cancelled before it runs, so the wake must reach the second.
        # A real release awaits its reset, which lets the waiter run first.
        pool._checked_out.pop(id(first))
        pool._idle.append(first)
        pool._wake_one()
        cancelled.cancel()
        self.assertIs(await asyncio.wait_for(patient, 2), first)


if __name__ == "__main__":
    unittest.main()
