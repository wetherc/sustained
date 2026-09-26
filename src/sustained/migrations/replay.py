"""
A recorded schema read, and the stand-in connection that answers it a
second time. The async migrator reads the schema through its adapter and
hands the blocking diff code this connection.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Sequence, cast

from sustained.types import Connection, Cursor, RowValue


def _is_read_savepoint(operation: str) -> bool:
    """Whether a statement is one of the savepoints a guarded read takes."""
    from sustained.introspect import _READ_SAVEPOINT

    return operation.strip().rstrip(";").upper().endswith(
        _READ_SAVEPOINT.upper()
    ) and operation.strip().upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))


class _ReplayCursor:
    """The cursor a SchemaRead hands out: it answers from the recording."""

    def __init__(self, steps: Sequence["_ReadStep"]) -> None:
        self._steps = steps
        self._position = 0
        self._rows: List[Sequence[RowValue]] = []

    @property
    def description(self) -> Optional[Sequence[object]]:
        return None

    @property
    def rowcount(self) -> int:
        return len(self._rows)

    def execute(self, operation: str, parameters: Sequence[object] = (), /) -> object:
        self._rows = []
        if _is_read_savepoint(operation):
            # A guarded read takes a savepoint around each statement, to
            # keep one failed query from carrying away the transaction.
            # A replay runs no transaction and re-raises a recorded error
            # on its own, so the savepoint has nothing to protect.
            return None
        if self._position >= len(self._steps):
            raise ValueError(
                "The recorded schema read has no answer for this statement: "
                f"{operation}"
            )
        step = self._steps[self._position]
        if step.sql != operation:
            raise ValueError(
                "The recorded schema read holds a different statement at "
                f"position {self._position}. Recorded: {step.sql} Asked: "
                f"{operation}"
            )
        self._position += 1
        if step.error is not None:
            raise step.error
        self._rows = step.rows
        return None

    def executemany(
        self, operation: str, seq_of_parameters: Sequence[Sequence[object]], /
    ) -> object:
        raise ValueError("A recorded schema read runs one statement at a time.")

    def fetchone(self) -> Optional[Sequence[RowValue]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> Sequence[Sequence[RowValue]]:
        return self._rows

    def close(self) -> None:
        return None


class _ReadStep(NamedTuple):
    """One statement of a recorded schema read and what it answered."""

    sql: str
    rows: List[Sequence[RowValue]]
    error: Optional[Exception]


class SchemaRead:
    """
    One recorded read of a database's schema, and a stand-in connection
    that answers it a second time.

    autogenerate() and diff_schema() read the schema through a blocking
    connection. The async migrator holds an adapter instead, so it reads
    the schema through the adapter, records every statement and the rows
    that came back, and hands those functions this connection. The
    reading code asks its next statement from the rows the last one
    returned, so a replay asks the same statements in the same order and
    reads the same answers. A statement that raised raises again.

    Nothing here executes anything. A connection that is asked for a
    statement the recording does not hold, or for a statement that
    differs from the one recorded at that position, raises ValueError. A
    replay that reads a differently scoped schema is a wrong answer, not
    a slower one, so it stops instead.
    """

    def __init__(self) -> None:
        self._steps: List[_ReadStep] = []

    def record(
        self,
        sql: str,
        rows: List[Sequence[RowValue]],
        error: Optional[Exception] = None,
    ) -> None:
        """Adds one statement of the read and what the database said."""
        self._steps.append(_ReadStep(sql, rows, error))

    def connection(self) -> Connection:
        """A connection stand-in that answers the recorded read."""
        return cast(Connection, _ReplayConnection(self._steps))


class _ReplayConnection:
    """The connection stand-in SchemaRead.connection() returns."""

    def __init__(self, steps: Sequence[_ReadStep]) -> None:
        self._steps = steps

    def cursor(self) -> Cursor:
        return cast(Cursor, _ReplayCursor(self._steps))

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None
