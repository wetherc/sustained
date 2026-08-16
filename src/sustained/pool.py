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
from typing import Callable, Iterator

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

    @property
    def size(self) -> int:
        """The number of connections the pool has created."""
        return self._created

    def acquire_raw(self) -> Connection:
        """
        Checks out a connection without a context manager. The caller must
        release() it; prefer connection() which guarantees the release.
        """
        if self._closed:
            raise RuntimeError("The connection pool is closed.")
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            can_create = self._created < self._max_size
            if can_create:
                self._created += 1
        if can_create:
            try:
                return self._factory()
            except BaseException:
                with self._lock:
                    self._created -= 1
                raise
        try:
            return self._idle.get(timeout=self._timeout)
        except queue.Empty:
            raise PoolTimeout(
                f"No connection available within {self._timeout} seconds "
                f"(pool size {self._max_size})."
            ) from None

    def release(self, connection: Connection) -> None:
        """Returns a checked-out connection to the pool."""
        if self._closed:
            self._close_connection(connection)
            return
        self._idle.put(connection)

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
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._close_connection(conn)

    @staticmethod
    def _close_connection(connection: Connection) -> None:
        connection.close()
