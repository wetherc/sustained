"""
A task cancelled while it gives an adapter back to AsyncConnectionPool
does not lose the adapter or its slot.
"""

import asyncio
import unittest

from sustained.aio import AsyncAdapter
from sustained.aio_pool import AsyncConnectionPool


class SlowRollbackAdapter(AsyncAdapter):
    """An adapter whose rollback waits until the test lets it finish."""

    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = False

    async def rollback(self):
        self.started.set()
        await self.finish.wait()

    async def close(self):
        self.closed = True


class TestPoolReleaseCancellation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.made = []

        async def factory():
            adapter = SlowRollbackAdapter()
            self.made.append(adapter)
            return adapter

        self.pool = AsyncConnectionPool(factory, max_size=1, timeout=0.5)

    async def test_a_cancelled_release_still_returns_the_adapter(self):
        adapter = await self.pool.acquire()
        release = asyncio.ensure_future(self.pool.release(adapter))
        await adapter.started.wait()
        release.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await release
        adapter.finish.set()
        again = await self.pool.acquire()
        self.assertIs(again, adapter)
        self.assertEqual(self.pool.size, 1)
        self.assertEqual(len(self.made), 1)

    async def test_a_task_cancelled_inside_scope_frees_its_slot(self):
        entered = asyncio.Event()

        async def worker():
            async with self.pool.scope():
                entered.set()
                await asyncio.sleep(10)

        task = asyncio.ensure_future(worker())
        await entered.wait()
        task.cancel()
        # A second cancellation lands while the release rolls back.
        await self.made[0].started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.made[0].finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        again = await self.pool.acquire()
        self.assertIs(again, self.made[0])


if __name__ == "__main__":
    unittest.main()
