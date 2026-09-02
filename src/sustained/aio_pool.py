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
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, Dict, List, Sequence, Tuple

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
        self._idle: "asyncio.Queue[AsyncAdapter]" = asyncio.Queue()
        self._created = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._checked_out: Dict[int, AsyncAdapter] = {}

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
        async with self._lock:
            self._checked_out[id(adapter)] = adapter
        return adapter

    async def _take(self) -> AsyncAdapter:
        """An idle adapter, a new one, or one another task gives back."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("The connection pool is closed.")
            if not self._idle.empty():
                return self._idle.get_nowait()
            if self._created < self._max_size:
                # Opening under the lock keeps two tasks from both deciding
                # there is room and taking the pool past max_size.
                adapter = await self._factory()
                self._created += 1
                return adapter
        try:
            return await asyncio.wait_for(self._idle.get(), self._timeout)
        except asyncio.TimeoutError:
            raise PoolTimeout(
                f"No adapter became free within {self._timeout} seconds."
            ) from None

    async def release(self, adapter: AsyncAdapter) -> None:
        """
        Gives a checked-out adapter back. The adapter is rolled back first,
        so a statement that failed or left a transaction open does not
        reach the next task. An adapter this pool did not hand out is
        refused, which is what catches a double release.

        Raises:
            ValueError: If the adapter is not checked out of this pool.
        """
        async with self._lock:
            if self._checked_out.pop(id(adapter), None) is None:
                raise ValueError("That adapter is not checked out of this pool.")
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
        async with self._lock:
            if self._closed:
                await adapter.close()
                self._created -= 1
                return
            self._idle.put_nowait(adapter)

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
        try:
            await adapter.close()
        except Exception:
            pass
        async with self._lock:
            self._created -= 1

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
        async with self._lock:
            self._closed = True
            idle: List[AsyncAdapter] = []
            while not self._idle.empty():
                idle.append(self._idle.get_nowait())
            self._created -= len(idle)
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
