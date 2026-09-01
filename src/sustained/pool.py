"""
A thread-safe connection pool for DB-API 2.0 connections.

Bind a pool with Model.bind(pool) and every run() checks a connection out
for the duration of the statement, including its eager loads. A
Model.transaction() block pins one checked-out connection to the calling
thread so all statements in the block share the same transaction.
"""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Callable, Dict, Iterator, List

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
        self._idle: "queue.Queue[Connection]" = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()
        self._closed = False
        self._checked_out: Dict[int, Connection] = {}
        self._driver_rolls_back = True

    @property
    def size(self) -> int:
        """The number of connections the pool has created."""
        return self._created

    def acquire_raw(self) -> Connection:
        """
        Checks out a connection without a context manager. The caller must
        release() it; prefer connection() which guarantees the release.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("The connection pool is closed.")
        try:
            return self._check_out(self._idle.get_nowait())
        except queue.Empty:
            pass
        with self._lock:
            can_create = self._created < self._max_size
            if can_create:
                self._created += 1
        if can_create:
            try:
                return self._check_out(self._factory())
            except BaseException:
                with self._lock:
                    self._created -= 1
                raise
        try:
            return self._check_out(self._idle.get(timeout=self._timeout))
        except queue.Empty:
            raise PoolTimeout(
                f"No connection available within {self._timeout} seconds "
                f"(pool size {self._max_size})."
            ) from None

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
        self._idle.put(connection)

    def _check_out(self, connection: Connection) -> Connection:
        with self._lock:
            self._checked_out[id(connection)] = connection
        return connection

    def _reset(self, connection: Connection) -> bool:
        """
        Ends any transaction the caller left open. Returns False when the
        connection cannot be reset and must be discarded.
        """
        if not self._driver_rolls_back:
            return True
        if getattr(connection, "autocommit", False) is True:
            return True
        if getattr(connection, "in_transaction", None) is False:
            return True
        try:
            connection.rollback()
        except Exception:
            if not self._responds(connection):
                return False
            # The driver refuses rollback outside a transaction (duckdb)
            # rather than reporting a broken connection. Learn that once
            # and stop asking.
            self._driver_rolls_back = False
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
            drained: List[Connection] = []
            while True:
                try:
                    drained.append(self._idle.get_nowait())
                except queue.Empty:
                    break
            self._created -= len(drained)
        for conn in drained:
            self._close_connection(conn)

    @staticmethod
    def _close_connection(connection: Connection) -> None:
        connection.close()
