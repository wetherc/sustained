"""
A thread-safe connection pool for DB-API 2.0 connections.

Bind a pool with Model.bind(pool) and every run() checks a connection out
for the duration of the statement, including its eager loads. A
Model.transaction() block pins one checked-out connection to the calling
thread so all statements in the block share the same transaction.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Deque, Dict, Iterator, List

from sustained.types import Connection


class PoolTimeout(RuntimeError):
    """Raised when no connection becomes available within the timeout."""


class ConnectionPool:
    """
    Pools connections produced by a factory callable, creating them lazily
    up to max_size and reusing released ones.
    """

    def __init__(
        self,
        factory: Callable[[], Connection],
        max_size: int = 5,
        timeout: float = 30.0,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1.")
        self._factory = factory
        self._max_size = max_size
        self._timeout = timeout
        self._idle: Deque[Connection] = deque()
        self._created = 0
        # Guards the idle queue, the counters and the checked-out map.
        # Every release, discard and close notifies it, so a waiter wakes
        # for a freed slot as well as for a returned connection.
        self._lock = threading.Condition()
        self._closed = False
        self._checked_out: Dict[int, Connection] = {}

    @property
    def size(self) -> int:
        """The number of connections the pool has created."""
        return self._created

    def acquire_raw(self) -> Connection:
        """
        Checks out a connection without a context manager. The caller must
        release() it; prefer connection() which guarantees the release.
        """
        deadline = time.monotonic() + self._timeout
        with self._lock:
            # A discarded connection frees a slot without putting anything
            # in the idle queue, so every wake-up checks both again. A
            # waiter that watched the queue alone would time out while the
            # pool had room to open a new connection.
            while True:
                if self._closed:
                    raise RuntimeError("The connection pool is closed.")
                if self._idle:
                    connection = self._idle.popleft()
                    self._checked_out[id(connection)] = connection
                    return connection
                if self._created < self._max_size:
                    self._created += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolTimeout(
                        f"No connection available within {self._timeout} "
                        f"seconds (pool size {self._max_size})."
                    )
                self._lock.wait(remaining)
        try:
            connection = self._factory()
        except BaseException:
            with self._lock:
                self._created -= 1
                self._lock.notify()
            raise
        with self._lock:
            self._checked_out[id(connection)] = connection
        return connection

    def release(self, connection: Connection) -> None:
        """
        Returns a checked-out connection to the pool. Any open transaction
        is rolled back first, so the next caller never inherits a stale
        snapshot or an aborted transaction. A connection this pool did not
        hand out raises ValueError; a connection that cannot be rolled back
        is closed and dropped.
        """
        with self._lock:
            if self._checked_out.pop(id(connection), None) is None:
                raise ValueError(
                    "This connection is not checked out of this pool. "
                    "Release each connection once, to the pool that "
                    "acquired it."
                )
            closed = self._closed
        if closed or not self._reset(connection):
            self._discard(connection)
            return
        with self._lock:
            # close() may have run during the reset. A connection put in
            # the idle queue after it drained would stay open for good.
            if not self._closed:
                self._idle.append(connection)
                self._lock.notify()
                return
        self._discard(connection)

    def _reset(self, connection: Connection) -> bool:
        """
        Ends any transaction the caller left open. Returns False when the
        connection cannot be reset and must be discarded.
        """
        if getattr(connection, "autocommit", False) is True:
            return True
        if getattr(connection, "in_transaction", None) is False:
            return True
        try:
            connection.rollback()
        except Exception:
            # The driver refuses rollback outside a transaction (duckdb)
            # rather than reporting a broken connection. The probe tells
            # the two apart. The rollback is still attempted on every
            # release, because a driver that raised once with nothing to
            # roll back can have a real transaction open the next time.
            if not self._responds(connection):
                return False
        return True

    @staticmethod
    def _responds(connection: Connection) -> bool:
        """Whether the connection still answers a trivial statement."""
        try:
            cursor = connection.cursor()
        except Exception:
            return False
        try:
            cursor.execute("SELECT 1")
            cursor.fetchall()
        except Exception:
            return False
        finally:
            try:
                cursor.close()
            except Exception:
                pass
        return True

    def _discard(self, connection: Connection) -> None:
        with self._lock:
            self._created -= 1
            self._lock.notify()
        try:
            self._close_connection(connection)
        except Exception:
            pass

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        """Checks out a connection for the duration of the block."""
        conn = self.acquire_raw()
        try:
            yield conn
        finally:
            self.release(conn)

    def close(self) -> None:
        """
        Closes the pool and every idle connection. Connections checked out
        at close time are closed when they are released.
        """
        with self._lock:
            self._closed = True
            drained: List[Connection] = list(self._idle)
            self._idle.clear()
            self._created -= len(drained)
            # Every waiting thread wakes to find the pool closed and raises.
            self._lock.notify_all()
        for conn in drained:
            self._close_connection(conn)

    @staticmethod
    def _close_connection(connection: Connection) -> None:
        connection.close()
