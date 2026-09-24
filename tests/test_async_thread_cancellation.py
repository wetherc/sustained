"""
A cancelled DbApiAsyncAdapter call keeps the adapter lock until its worker
thread ends, so no second call runs on the connection beside it.
"""

import asyncio
import threading
import unittest

from sustained.aio import DbApiAsyncAdapter


class BlockingConnection:
    """A DB-API connection whose statements wait on a threading.Event."""

    def __init__(self):
        self.started = threading.Event()
        self.finish = threading.Event()
        self.running = 0
        self.peak = 0
        self.guard = threading.Lock()

    def _enter(self):
        with self.guard:
            self.running += 1
            self.peak = max(self.peak, self.running)

    def _leave(self):
        with self.guard:
            self.running -= 1

    def cursor(self):
        return BlockingCursor(self)

    def rollback(self):
        self._enter()
        self._leave()


class BlockingCursor:
    description = None
    rowcount = 0

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        self.connection._enter()
        self.connection.started.set()
        self.connection.finish.wait(5)
        self.connection._leave()
        if sql == "FAIL":
            raise RuntimeError("the statement failed")

    def close(self):
        pass


class TestThreadCancellation(unittest.IsolatedAsyncioTestCase):
    async def run_cancelled(self, sql, cancels):
        conn = BlockingConnection()
        adapter = DbApiAsyncAdapter(conn)
        statement = asyncio.ensure_future(adapter.execute(sql, ()))
        await asyncio.to_thread(conn.started.wait, 5)
        for _ in range(cancels):
            statement.cancel()
            await asyncio.sleep(0.01)
        rollback = asyncio.ensure_future(adapter.rollback())
        await asyncio.sleep(0.05)
        self.assertFalse(rollback.done())
        conn.finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await statement
        await rollback
        self.assertEqual(conn.peak, 1)

    async def test_the_next_call_waits_for_the_cancelled_thread(self):
        await self.run_cancelled("SELECT 1", cancels=1)

    async def test_a_second_cancellation_still_waits(self):
        await self.run_cancelled("SELECT 1", cancels=2)

    async def test_the_thread_error_gives_way_to_the_cancellation(self):
        await self.run_cancelled("FAIL", cancels=1)


if __name__ == "__main__":
    unittest.main()
