"""
A pool of async adapters.

Bind a pool with Model.bind_async(pool) and every arun() checks one
adapter out for the length of the call, its eager loads and its commit
included, then gives it back. An async_transaction() block holds one
adapter from BEGIN to COMMIT.

A single adapter serializes: DbApiAsyncAdapter holds a lock across every
call, and one asyncpg connection runs one statement at a time. A pool is
how concurrent async queries reach the database in parallel.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Deque,
    Dict,
    List,
    Sequence,
    Set,
    Tuple,
)

from sustained.aio import AsyncAdapter
from sustained.pool import PoolTimeout
from sustained.types import RowValue, SqlValue

AsyncAdapterFactory = Callable[[], Awaitable[AsyncAdapter]]
"""Opens one new adapter. The pool awaits it when it needs another."""


class AsyncConnectionPool(AsyncAdapter):
    """
    Pools adapters produced by an async factory, opening them lazily up to
    max_size and reusing released ones.

    The pool is an AsyncAdapter so it can be bound and passed like one, but
    it runs no statement itself. Statements go to the adapter its scope()
    hands out; calling fetch() or execute() on the pool raises, because a
    write and its commit would land on two different connections.
    """

    def __init__(
        self,
        factory: AsyncAdapterFactory,
        max_size: int = 5,
        timeout: float = 30.0,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1.")
        self._factory = factory
        self._max_size = max_size
        self._timeout = timeout
        # The pool runs on one event loop and never awaits between reading
        # its counters and updating them, so it needs no lock.
        self._idle: Deque[AsyncAdapter] = deque()
        # Tasks waiting for an adapter or a free slot, oldest first.
        self._waiters: "Deque[asyncio.Future[None]]" = deque()
        self._created = 0
        self._closed = False
        self._checked_out: Dict[int, AsyncAdapter] = {}
        # Releases still running after their caller was cancelled. The set
        # keeps a strong reference, so a pending reset is not collected.
        self._resets: "Set[asyncio.Task[None]]" = set()

    @property
    def size(self) -> int:
        """The number of adapters the pool has opened."""
        return self._created

    async def acquire(self) -> AsyncAdapter:
        """
        Checks out an adapter without a context manager. The caller must
        release() it; prefer scope(), which gives it back for you.

        Raises:
            PoolTimeout: If no adapter becomes free within the timeout.
        """
        adapter = await self._take()
        # No await between the take and the record: a cancellation there
        # would lose the adapter with its slot still counted.
        self._checked_out[id(adapter)] = adapter
        return adapter

    async def _take(self) -> AsyncAdapter:
        """
        An idle adapter, a new one, or one another task gives back.

        The loop checks both again after every wake-up. A discarded
        adapter frees a slot without putting anything in the idle queue,
        so a waiter that watched the queue alone would time out while
        the pool had room to open a new adapter.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            if self._closed:
                raise RuntimeError("The connection pool is closed.")
            if self._idle:
                return self._idle.popleft()
            if self._created < self._max_size:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PoolTimeout(
                    f"No adapter became free within {self._timeout} seconds."
                )
            await self._wait(loop, remaining)
        # The slot is reserved before the factory runs, so two tasks cannot
        # both see room and take the pool past max_size. The factory runs
        # with nothing held, so a slow connect does not stop releases.
        self._created += 1
        try:
            return await self._factory()
        except BaseException:
            self._created -= 1
            self._wake_one()
            raise

    async def _wait(self, loop: asyncio.AbstractEventLoop, timeout: float) -> None:
        """Waits until a release or a discard wakes this task, or times out."""
        waiter: "asyncio.Future[None]" = loop.create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            pass
        except BaseException:
            # A wake-up that reached a cancelled task goes to the next one,
            # or the adapter it announced would sit idle beside a waiter.
            if waiter.done() and not waiter.cancelled():
                self._wake_one()
            raise
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _wake_one(self) -> None:
        """Wakes the oldest waiting task, if one is still waiting."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    async def release(self, adapter: AsyncAdapter) -> None:
        """
        Gives a checked-out adapter back. The adapter is rolled back first,
        so a statement that failed or left a transaction open does not
        reach the next task. An adapter this pool did not hand out is
        refused, which is what catches a double release.

        Raises:
            ValueError: If the adapter is not checked out of this pool.
        """
        if self._checked_out.pop(id(adapter), None) is None:
            raise ValueError("That adapter is not checked out of this pool.")
        # The reset runs in a task of its own under a shield. A caller
        # cancelled during the rollback gets its CancelledError at once,
        # and the reset still ends with the adapter back in the idle queue
        # or discarded. Cancelled in place, it would lose the adapter with
        # its slot still counted, and after max_size such cancellations
        # every acquire would raise PoolTimeout.
        task = asyncio.ensure_future(self._reset(adapter))
        self._resets.add(task)
        task.add_done_callback(self._resets.discard)
        await asyncio.shield(task)

    async def _reset(self, adapter: AsyncAdapter) -> None:
        """Rolls a released adapter back and returns it to the idle queue."""
        try:
            await adapter.rollback()
        except Exception:
            # Some drivers refuse rollback outside a transaction (duckdb)
            # rather than reporting a broken connection. The probe tells
            # the two apart, and only a connection that no longer answers
            # is dropped.
            if not await self._responds(adapter):
                await self._discard(adapter)
                return
        if self._closed:
            self._created -= 1
            await adapter.close()
            return
        self._idle.append(adapter)
        self._wake_one()

    @staticmethod
    async def _responds(adapter: AsyncAdapter) -> bool:
        """Whether the adapter still answers a trivial statement."""
        try:
            await adapter.fetch("SELECT 1", ())
        except Exception:
            return False
        return True

    async def _discard(self, adapter: AsyncAdapter) -> None:
        """Drops a broken adapter and frees its slot for a new one."""
        self._created -= 1
        self._wake_one()
        try:
            await adapter.close()
        except Exception:
            pass

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[AsyncAdapter]:
        """One adapter for the length of the block."""
        adapter = await self.acquire()
        try:
            yield adapter
        finally:
            await self.release(adapter)

    async def close(self) -> None:
        """
        Closes every idle adapter and refuses new checkouts. Adapters still
        checked out are closed when they are released.
        """
        self._closed = True
        idle: List[AsyncAdapter] = list(self._idle)
        self._idle.clear()
        self._created -= len(idle)
        # Every waiting task wakes to find the pool closed and raises.
        while self._waiters:
            self._wake_one()
        for adapter in idle:
            try:
                await adapter.close()
            except Exception:
                pass

    def _refuse(self) -> RuntimeError:
        return RuntimeError(
            "A pool runs no statement itself. Take an adapter out of it "
            "with 'async with pool.scope() as adapter', or bind the pool "
            "and let arun() do it."
        )

    async def fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        raise self._refuse()

    async def execute(self, sql: str, params: Tuple[SqlValue, ...]) -> int:
        raise self._refuse()

    async def executemany(
        self, sql: str, seq_of_params: List[Tuple[SqlValue, ...]]
    ) -> int:
        raise self._refuse()

    async def commit(self) -> None:
        raise self._refuse()

    async def rollback(self) -> None:
        raise self._refuse()
