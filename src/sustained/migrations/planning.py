"""
Planning a run without applying it: the migration a diff of the models
produces, the drift the models still report, and the script a run would
execute.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Sequence, Tuple, Type

from sustained.dialects import Dialects
from sustained.migrations.checks import _is_current
from sustained.migrations.migration import (
    AppliedRecord,
    Migration,
    migration_checksum,
    migration_sql,
)
from sustained.migrations.tracking import _next_seq, quoted_columns
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.introspect import Snapshot
    from sustained.model import Model


def generated_id(migration_id: Optional[str] = None) -> str:
    """The id a migration generated from the models carries."""
    return migration_id or datetime.now(timezone.utc).strftime("auto_%Y%m%d%H%M%S_%f")


def plan_migration(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects,
    exclude_tables: Tuple[str, ...],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
) -> Optional[Migration]:
    """
    The migration a diff of the models against the database produces, or
    None when the schema already holds everything the models declare.
    Both migrators plan through this. A snapshot already read is used
    instead of reading the schema again.
    """
    from sustained.autogenerate import autogenerate

    return autogenerate(
        connection,
        models,
        id=generated_id(migration_id),
        dialect=dialect,
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        exclude_tables=exclude_tables,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
    )


def drift_lines(
    connection: Connection,
    models: List[Type["Model"]],
    dialect: Dialects,
    exclude_tables: Tuple[str, ...],
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    ignore_changed_columns: bool = False,
    snapshot: Optional["Snapshot"] = None,
) -> List[str]:
    """
    What the models still ask for, one readable line each. Both migrators
    report drift through this. A snapshot already read is used instead of
    reading the database again.
    """
    from sustained.autogenerate import diff_schema

    diff = diff_schema(
        connection,
        models,
        dialect=dialect,
        exclude_tables=exclude_tables,
        renames=renames,
        table_renames=table_renames,
        snapshot=snapshot,
    )
    return diff.outstanding(ignore_changed_columns=ignore_changed_columns)


def render_script(
    compiler: "Compiler",
    table_sql: str,
    migrations: Sequence[Migration],
    records: Sequence[AppliedRecord],
    direction: str = "up",
    generated: Optional[Mapping[str, Migration]] = None,
) -> str:
    """
    The SQL a run would execute, rendered from the migrations and the
    tracking rows that were read, without touching a database. Both
    migrators call this, so either one renders the same script.

    `generated` maps the id of each migration generated from the models
    to the migration its tracking row stores. A 'down' script reverts
    those from the stored statements, the same way down() does, and
    stops at an applied id found in neither place.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    format_value = compiler.format_value
    column = compiler.quote_identifier
    insert_columns = quoted_columns(
        compiler, "id", "seq", "checksum", "applied_at", "execution_ms", "success"
    )
    versioned = [m for m in migrations if not m.repeatable]
    repeatables = [m for m in migrations if m.repeatable]
    lines: List[str] = []
    if direction == "up":
        records_by_id = {r.id: r for r in records}
        applied = {r.id for r in records if r.success}
        next_seq = _next_seq(list(records))
        for migration in versioned:
            if migration.id in applied:
                continue
            lines.append(f"-- up: {migration.id}")
            lines.extend(f"{s};" for s in migration_sql(migration, "up", compiler))
            lines.append(
                f"INSERT INTO {table_sql} "
                f"({insert_columns}) "
                f"VALUES ({format_value(migration.id)}, {next_seq}, "
                f"{format_value(migration_checksum(migration))}, "
                f"{format_value(timestamp)}, NULL, "
                f"{compiler.compile_boolean(True)});"
            )
            next_seq += 1
        for migration in repeatables:
            record = records_by_id.get(migration.id)
            checksum = migration_checksum(migration)
            if _is_current(record, migration, True):
                continue
            lines.append(f"-- repeat: {migration.id}")
            lines.extend(f"{s};" for s in migration_sql(migration, "up", compiler))
            if record is None:
                lines.append(
                    f"INSERT INTO {table_sql} "
                    f"({insert_columns}) "
                    f"VALUES ({format_value(migration.id)}, {next_seq}, "
                    f"{format_value(checksum)}, "
                    f"{format_value(timestamp)}, NULL, "
                    f"{compiler.compile_boolean(True)});"
                )
                next_seq += 1
            else:
                lines.append(
                    f"UPDATE {table_sql} "
                    f"SET {column('checksum')} = {format_value(checksum)}, "
                    f"{column('applied_at')} = "
                    f"{format_value(timestamp)}, "
                    f"{column('execution_ms')} = NULL, "
                    f"{column('success')} = "
                    f"{compiler.compile_boolean(True)} "
                    f"WHERE {column('id')} = {format_value(migration.id)};"
                )
    elif direction == "down":
        by_id = {m.id: m for m in migrations}
        repeatable_ids = {m.id for m in repeatables}
        applied_ids = [
            r.id for r in records if r.success and r.id not in repeatable_ids
        ]
        stored = generated or {}
        for migration_id in reversed(applied_ids):
            registered = by_id.get(migration_id) or stored.get(migration_id)
            if registered is None or registered.down is None:
                lines.append(
                    f"-- down: {migration_id} has no reversible step; stopping"
                )
                break
            lines.append(f"-- down: {migration_id}")
            lines.extend(f"{s};" for s in migration_sql(registered, "down", compiler))
            lines.append(
                f"DELETE FROM {table_sql} WHERE {column('id')} = "
                f"{format_value(migration_id)};"
            )
    else:
        raise ValueError("direction must be 'up' or 'down'.")
    return "\n".join(lines)
