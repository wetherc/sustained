"""
The tracking table and the rehearsal table: their columns, the SQL that
reads and writes their rows, and the messages around the migration lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence

from sustained.migrations.migration import AppliedRecord
from sustained.types import Cursor, RowValue

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.schema import ColumnDef


# Columns added when upgrading a tracking table written by an earlier
# version, which held only id and applied_at.
_UPGRADE_COLUMNS = (
    "seq",
    "checksum",
    "execution_ms",
    "success",
    "generated",
    "steps",
)


def quoted_columns(compiler: "Compiler", *names: str) -> str:
    """
    Tracking table column names, quoted for the dialect. Quoting is not
    cosmetic here: `generated` is a reserved word on MySQL, so a bare
    reference to it is a syntax error.
    """
    return ", ".join(compiler.quote_identifier(name) for name in names)


def records_select(compiler: "Compiler", table_sql: str) -> str:
    """Reads every tracking table row in application order."""
    columns = quoted_columns(compiler, "id", "seq", "checksum", "success", "generated")
    order = quoted_columns(compiler, "seq", "applied_at", "id")
    return f"SELECT {columns} FROM {table_sql} ORDER BY {order}"


def insert_sql(compiler: "Compiler", table_sql: str) -> str:
    """Writes one tracking table row."""
    columns = quoted_columns(
        compiler,
        "id",
        "seq",
        "checksum",
        "applied_at",
        "execution_ms",
        "success",
        "generated",
        "steps",
    )
    values = ", ".join([compiler.placeholder()] * 8)
    return f"INSERT INTO {table_sql} ({columns}) VALUES ({values})"


def update_sql(compiler: "Compiler", table_sql: str) -> str:
    """Rewrites the tracking table row a repeatable already has."""
    placeholder = compiler.placeholder()
    column = compiler.quote_identifier
    assignments = ", ".join(
        f"{column(name)} = {placeholder}"
        for name in (
            "checksum",
            "applied_at",
            "execution_ms",
            "success",
            "generated",
            "steps",
        )
    )
    return (
        f"UPDATE {table_sql} SET {assignments} " f"WHERE {column('id')} = {placeholder}"
    )


def _tracking_column_defs(constraints: bool) -> Dict[str, "ColumnDef"]:
    """
    The tracking table's columns. Engines without constraints, such as
    Athena, get plain nullable columns; the migrator never writes a
    duplicate id.
    """
    from sustained.schema import Boolean, Integer, String, Text

    if constraints:
        return {
            "id": String(255, primary_key=True),
            "seq": Integer(),
            "checksum": String(64),
            "applied_at": Text(nullable=False),
            "execution_ms": Integer(),
            "success": Boolean(nullable=False),
            "generated": Boolean(),
            "steps": Text(),
        }
    return {
        "id": String(255),
        "seq": Integer(),
        "checksum": String(64),
        "applied_at": Text(),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
        "steps": Text(),
    }


def _rehearsal_column_defs(constraints: bool) -> Dict[str, "ColumnDef"]:
    """
    The rehearsal table's columns: the key a rehearsal earned, what it
    proved, and when. Engines without constraints get plain nullable
    columns, as the tracking table does.
    """
    from sustained.schema import String, Text

    if constraints:
        return {
            "rehearsal_key": String(64, primary_key=True),
            "outcome": String(16, nullable=False),
            "rehearsed_at": Text(nullable=False),
        }
    return {
        "rehearsal_key": String(64),
        "outcome": String(16),
        "rehearsed_at": Text(),
    }


def _upgrade_column_def(name: str) -> "ColumnDef":
    """A nullable definition for one upgrade column, safe to ADD COLUMN."""
    from sustained.schema import Boolean, Integer, String, Text

    defs: Dict[str, "ColumnDef"] = {
        "seq": Integer(),
        "checksum": String(64),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
        "steps": Text(),
    }
    return defs[name]


def records_from_rows(rows: Iterable[Sequence[RowValue]]) -> List[AppliedRecord]:
    """The tracking table rows a records_select() read returned."""
    return [
        AppliedRecord(str(row[0]), row[1], row[2], bool(row[3]), bool(row[4]))
        for row in rows
    ]


def _next_seq(records: List[AppliedRecord]) -> int:
    return 1 + max((r.seq or 0 for r in records), default=0)


def _lock_row(cursor: "Cursor") -> Optional[Sequence[object]]:
    """
    The row a lock statement returned, or None when it returned nothing.
    A driver whose lock statement produces no result set raises on the
    fetch, which reads as no row.
    """
    try:
        row = cursor.fetchone()
    except Exception:
        return None
    return None if row is None else tuple(row)


def _lock_message(table: str, problem: str) -> str:
    """The error for a lock the engine did not grant."""
    return (
        f"The migration lock for '{table}' was not granted: {problem}. "
        "Another migrator may be running; wait for it to finish and try "
        "again."
    )


def _unlock_message(table: str, error: Exception) -> str:
    """The error for a lock the engine did not release."""
    return (
        f"The migration lock for '{table}' was not released: {error!r}. "
        "The lock stays held until this connection closes, and other "
        "migrators wait for it until then. Close the connection."
    )
