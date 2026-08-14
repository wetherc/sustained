"""
Explicit, ordered schema migrations.

A Migration pairs an id with an up step and an optional down step. Steps
are a SQL string, a list of SQL strings, or a callable that receives the
connection. The Migrator applies pending migrations in order, records each
applied id in a tracking table, and reverts through the down steps.

The tracking table stores one row per applied migration: the id, a
monotonic sequence number, a SHA-256 checksum of the up step, the apply
timestamp, the execution time in milliseconds, and a success flag.
Tracking tables written by earlier versions of Sustained, which held only
the id and the timestamp, are upgraded in place on first use.

Migrations are written by hand, generated from a model with
create_table_migration(), or produced by schema diffing through
sustained.autogenerate and Migrator.sync().
"""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    Union,
)

from sustained.dialects import Dialects
from sustained.execution import transaction

if TYPE_CHECKING:
    from sustained.compilers.base import Compiler
    from sustained.model import Model
    from sustained.schema import ColumnDef

MigrationStep = Union[str, List[str], Callable[[Any], None]]


class Migration:
    """
    One schema change with an id, an up step, and an optional down step.

    A checksum may be supplied for callable steps, whose SQL cannot be
    hashed; validation then compares it like a computed one.
    """

    def __init__(
        self,
        id: str,
        up: MigrationStep,
        down: Optional[MigrationStep] = None,
        checksum: Optional[str] = None,
    ) -> None:
        if not id:
            raise ValueError("A migration needs a non-empty id.")
        self.id = id
        self.up = up
        self.down = down
        self.checksum = checksum


def migration_checksum(migration: Migration) -> Optional[str]:
    """
    The SHA-256 hex digest of a migration's up statements, each stripped of
    surrounding whitespace. Callable steps have no SQL to hash and return
    the migration's explicit checksum, or None when it has none.
    """
    if migration.checksum is not None:
        return migration.checksum
    step = migration.up
    if callable(step):
        return None
    statements = [step] if isinstance(step, str) else list(step)
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


class AppliedRecord(NamedTuple):
    """One row of the tracking table."""

    id: str
    seq: Optional[int]
    checksum: Optional[str]
    success: bool


# Columns added when upgrading a tracking table written by an earlier
# version, which held only id and applied_at.
_UPGRADE_COLUMNS = ("seq", "checksum", "execution_ms", "success")

_RECORDS_SELECT = "SELECT id, seq, checksum, success FROM"


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
        }
    return {
        "id": String(255),
        "seq": Integer(),
        "checksum": String(64),
        "applied_at": Text(),
        "execution_ms": Integer(),
        "success": Boolean(),
    }


def _upgrade_column_def(name: str) -> "ColumnDef":
    """A nullable definition for one upgrade column, safe to ADD COLUMN."""
    from sustained.schema import Boolean, Integer, String

    defs: Dict[str, "ColumnDef"] = {
        "seq": Integer(),
        "checksum": String(64),
        "execution_ms": Integer(),
        "success": Boolean(),
    }
    return defs[name]


def create_table_migration(model: Type["Model"]) -> Migration:
    """
    Builds a migration that creates the model's table from its tableColumns
    on the way up and drops it on the way down. The migration id is
    'create_<tableName>'.
    """
    return Migration(
        id=f"create_{model.tableName}",
        up=model.create_table_statements(),
        down=model.drop_table_sql(),
    )


def migration_sql(migration: Migration, direction: str = "up") -> List[str]:
    """
    Renders a migration's statements for offline review. Callable steps
    cannot be rendered and appear as a comment.
    """
    step = migration.up if direction == "up" else migration.down
    if step is None:
        raise ValueError(f"Migration '{migration.id}' has no {direction} step.")
    if callable(step):
        return [f"-- migration '{migration.id}': callable step, run online"]
    return [step] if isinstance(step, str) else list(step)


def _run_step(connection: Any, step: MigrationStep) -> None:
    if callable(step):
        step(connection)
        return
    statements = [step] if isinstance(step, str) else list(step)
    cursor = connection.cursor()
    for statement in statements:
        cursor.execute(statement)


def _next_seq(records: List[AppliedRecord]) -> int:
    return 1 + max((r.seq or 0 for r in records), default=0)


def _validation_problems(
    migrations: List[Migration],
    records: List[AppliedRecord],
    allow_out_of_order: bool = False,
    require_registered: bool = True,
) -> List[str]:
    """
    Compares the registered migrations against the tracking table rows and
    describes every inconsistency: failed attempts, applied migrations the
    migrator does not know, edited migrations whose checksum no longer
    matches, and pending migrations ordered before applied ones. Passing
    require_registered=False skips the unknown-id check, for sync() runs
    whose registry holds only the migration generated from the diff.
    """
    problems: List[str] = []
    registered = {m.id: m for m in migrations}
    applied_ids = {r.id for r in records if r.success}

    for record in records:
        if not record.success:
            problems.append(
                f"migration '{record.id}' has a failed attempt on record; "
                "clean up any partial changes, then run repair() and retry"
            )
    for record in records:
        if not record.success:
            continue
        migration = registered.get(record.id)
        if migration is None:
            if require_registered:
                problems.append(
                    f"applied migration '{record.id}' is not registered "
                    "with this migrator"
                )
            continue
        current = migration_checksum(migration)
        if (
            current is not None
            and record.checksum is not None
            and current != record.checksum
        ):
            problems.append(
                f"checksum mismatch for '{record.id}': the migration "
                "changed after it was applied; restore it, or run repair() "
                "to accept the new contents"
            )
    if not allow_out_of_order:
        first_pending: Optional[str] = None
        for migration in migrations:
            if migration.id not in applied_ids:
                if first_pending is None:
                    first_pending = migration.id
            elif first_pending is not None:
                problems.append(
                    f"pending migration '{first_pending}' is ordered before "
                    f"applied migration '{migration.id}'; pass "
                    "allow_out_of_order=True to apply it anyway"
                )
                break
    return problems


class Migrator:
    """
    Applies and reverts an ordered list of migrations on one connection.

    Applied migration ids live in a tracking table, created on first use.
    Each migration runs inside a transaction, so a failing step leaves the
    schema at the previous migration. Engines that do not support
    transactional DDL may still leave partial changes from a multi-step
    migration. Engines without transactions at all, such as Athena, run
    each step bare; a failing migration there can leave partial changes
    that need manual cleanup.
    """

    def __init__(
        self,
        connection: Any,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
        tracking_table_options: Optional[Any] = None,
    ) -> None:
        ids = [m.id for m in migrations]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate migration ids: {sorted(duplicates)}.")
        self._connection = connection
        self._migrations = list(migrations)
        self._table = table
        self._dialect = dialect
        self._compiler: "Compiler" = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    @contextmanager
    def _migration_scope(self) -> Iterator[None]:
        """
        A transaction on engines that have them; a bare run followed by a
        commit (when the driver has one) on engines that do not.
        """
        if self._compiler.supports_transactions():
            with transaction(self._connection):
                yield
            return
        yield
        self._commit_quietly()

    def _commit_quietly(self) -> None:
        if hasattr(self._connection, "commit"):
            self._connection.commit()

    def _rollback_quietly(self) -> None:
        try:
            if hasattr(self._connection, "rollback"):
                self._connection.rollback()
        except Exception:
            pass

    @contextmanager
    def _lock_scope(self) -> Iterator[None]:
        """
        Holds the engine's advisory lock, named after the tracking table,
        for the duration of a run, so concurrent migrators queue instead of
        racing. A no-op on engines without one.
        """
        lock_statements = self._compiler.migration_lock_sql(self._table)
        if not lock_statements:
            yield
            return
        cursor = self._connection.cursor()
        for statement in lock_statements:
            cursor.execute(statement)
        try:
            yield
        finally:
            for statement in self._compiler.migration_unlock_sql(self._table):
                try:
                    self._connection.cursor().execute(statement)
                except Exception:
                    pass

    def _ensure_tracking_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._tracking_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._table_sql(),
            _tracking_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        self._connection.cursor().execute(sql)
        self._commit_quietly()
        self._upgrade_tracking_table()
        self._tracking_ready = True

    def _has_columns(self, columns: Tuple[str, ...]) -> bool:
        """Probes the tracking table for the given columns."""
        try:
            cursor = self._connection.cursor()
            cursor.execute(
                f"SELECT {', '.join(columns)} FROM {self._table_sql()} WHERE 1 = 0"
            )
            cursor.fetchall()
            return True
        except Exception:
            # A failed probe can poison an open transaction (Postgres
            # aborts it), so clear the slate before the next statement.
            self._rollback_quietly()
            return False

    def _upgrade_tracking_table(self) -> None:
        """
        Brings a tracking table written by an earlier version, which held
        only id and applied_at, up to the current shape. Missing columns
        are added nullable; seq and success are backfilled from the
        existing rows in applied order.
        """
        from sustained.schema import render_column_sql

        if self._has_columns(_UPGRADE_COLUMNS):
            return
        added: List[str] = []
        for name in _UPGRADE_COLUMNS:
            if self._has_columns((name,)):
                continue
            column_sql = render_column_sql(
                self._compiler, name, _upgrade_column_def(name), inline_pk=False
            )
            statement = self._compiler.compile_add_column(self._table_sql(), column_sql)
            self._connection.cursor().execute(statement)
            added.append(name)
        self._commit_quietly()
        if "seq" not in added and "success" not in added:
            return
        cursor = self._connection.cursor()
        cursor.execute(f"SELECT id FROM {self._table_sql()} ORDER BY applied_at, id")
        ids = [row[0] for row in cursor.fetchall()]
        placeholder = self._compiler.placeholder()
        for position, migration_id in enumerate(ids, start=1):
            cursor.execute(
                f"UPDATE {self._table_sql()} SET seq = {placeholder}, "
                f"success = {placeholder} WHERE id = {placeholder}",
                (position, True, migration_id),
            )
        self._commit_quietly()

    def applied_records(self) -> List[AppliedRecord]:
        """Returns every tracking table row in application order."""
        self._ensure_tracking_table()
        cursor = self._connection.cursor()
        cursor.execute(
            f"{_RECORDS_SELECT} {self._table_sql()} ORDER BY seq, applied_at, id"
        )
        return [
            AppliedRecord(row[0], row[1], row[2], bool(row[3]))
            for row in cursor.fetchall()
        ]

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in self.applied_records() if r.success]

    def pending(self) -> List[Migration]:
        """Returns the registered migrations that have not been applied."""
        applied = set(self.applied())
        return [m for m in self._migrations if m.id not in applied]

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def _insert_sql(self) -> str:
        placeholder = self._compiler.placeholder()
        values = ", ".join([placeholder] * 6)
        return (
            f"INSERT INTO {self._table_sql()} "
            f"(id, seq, checksum, applied_at, execution_ms, success) "
            f"VALUES ({values})"
        )

    def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        from sustained.exceptions import MigrationError

        problems = _validation_problems(self._migrations, self.applied_records())
        if problems and raise_on_problems:
            raise MigrationError(problems)
        return problems

    def repair(self) -> List[str]:
        """
        Brings the tracking table back in line with the registered
        migrations: deletes rows left by failed attempts and rewrites
        stored checksums that no longer match, including null checksums on
        rows written before checksums existed. Returns a description of
        every action taken. Schema changes a failed attempt left behind
        are not touched; clean those up first.
        """
        records = self.applied_records()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        cursor = self._connection.cursor()
        actions: List[str] = []
        for record in records:
            if not record.success:
                cursor.execute(
                    f"DELETE FROM {self._table_sql()} WHERE id = {placeholder} "
                    f"AND success = {self._compiler.compile_boolean(False)}",
                    (record.id,),
                )
                actions.append(f"removed the failed attempt of '{record.id}'")
                continue
            migration = by_id.get(record.id)
            if migration is None:
                continue
            current = migration_checksum(migration)
            if current is not None and current != record.checksum:
                cursor.execute(
                    f"UPDATE {self._table_sql()} SET checksum = {placeholder} "
                    f"WHERE id = {placeholder}",
                    (current, record.id),
                )
                actions.append(f"updated the stored checksum of '{record.id}'")
        self._commit_quietly()
        return actions

    def _record_failure(self, migration: Migration, seq: int) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine without transactions, where partial changes may remain. A
        failure to write the row never masks the original error.
        """
        if self._compiler.supports_transactions():
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            self._connection.cursor().execute(
                self._insert_sql(),
                (
                    migration.id,
                    seq,
                    migration_checksum(migration),
                    timestamp,
                    None,
                    False,
                ),
            )
            self._commit_quietly()
        except Exception:
            pass

    def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
    ) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.

        The run validates first: failed attempts, unknown applied ids,
        checksum mismatches, and out-of-order pending migrations all stop
        it. Pass validate=False to skip the checks, or
        allow_out_of_order=True to accept a pending migration that is
        ordered before an applied one.
        """
        from sustained.exceptions import MigrationError

        migrations = self._migrations
        if target is not None:
            ids = [m.id for m in migrations]
            if target not in ids:
                raise ValueError(f"Unknown migration target: {target!r}.")
            migrations = migrations[: ids.index(target) + 1]

        with self._lock_scope():
            records = self.applied_records()
            if validate:
                problems = _validation_problems(
                    self._migrations, records, allow_out_of_order
                )
                if problems:
                    raise MigrationError(problems)
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            applied_now: List[str] = []
            for migration in migrations:
                if migration.id in already_applied:
                    continue
                try:
                    with self._migration_scope():
                        started = time.perf_counter()
                        _run_step(self._connection, migration.up)
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        timestamp = datetime.now(timezone.utc).isoformat()
                        self._connection.cursor().execute(
                            self._insert_sql(),
                            (
                                migration.id,
                                next_seq,
                                migration_checksum(migration),
                                timestamp,
                                elapsed_ms,
                                True,
                            ),
                        )
                except Exception:
                    self._record_failure(migration, next_seq)
                    raise
                next_seq += 1
                applied_now.append(migration.id)
            return applied_now

    def sync(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> List[str]:
        """
        Diffs the database against the models, registers the generated
        migration, and applies everything pending. Returns the applied ids;
        an empty list means the schema was already up to date.

        Additive changes generate reversible steps, so down() rolls the
        sync back. Drops require allow_drops=True and are not reversible.
        """
        from sustained.autogenerate import autogenerate

        with self._lock_scope():
            self._ensure_tracking_table()
            generated_id = migration_id or datetime.now(timezone.utc).strftime(
                "auto_%Y%m%d%H%M%S_%f"
            )
            migration: Optional[Migration] = autogenerate(
                self._connection,
                models,
                id=generated_id,
                dialect=self._dialect,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                exclude_tables=(self._table,),
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
            )
            if migration is not None:
                self._migrations.append(migration)
            problems = _validation_problems(
                self._migrations,
                self.applied_records(),
                allow_out_of_order=True,
                require_registered=False,
            )
            if problems:
                from sustained.exceptions import MigrationError

                raise MigrationError(problems)
            return self.up(validate=False)

    def script(self, direction: str = "up") -> str:
        """
        Renders the SQL a run would execute, without executing anything,
        for review or DBA handoff. 'up' renders every pending migration;
        'down' renders the applied migrations newest-first. Tracking table
        bookkeeping statements are included.
        """
        placeholder_free_ts = datetime.now(timezone.utc).isoformat()
        format_value = self._compiler.format_value
        lines: List[str] = []
        if direction == "up":
            records = self.applied_records()
            applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            for migration in self._migrations:
                if migration.id in applied:
                    continue
                lines.append(f"-- up: {migration.id}")
                lines.extend(f"{s};" for s in migration_sql(migration, "up"))
                lines.append(
                    f"INSERT INTO {self._table_sql()} "
                    f"(id, seq, checksum, applied_at, execution_ms, success) "
                    f"VALUES ({format_value(migration.id)}, {next_seq}, "
                    f"{format_value(migration_checksum(migration))}, "
                    f"{format_value(placeholder_free_ts)}, NULL, "
                    f"{self._compiler.compile_boolean(True)});"
                )
                next_seq += 1
        elif direction == "down":
            by_id = {m.id: m for m in self._migrations}
            for migration_id in reversed(self.applied()):
                registered = by_id.get(migration_id)
                if registered is None or registered.down is None:
                    lines.append(
                        f"-- down: {migration_id} has no reversible step; stopping"
                    )
                    break
                lines.append(f"-- down: {migration_id}")
                lines.extend(f"{s};" for s in migration_sql(registered, "down"))
                lines.append(
                    f"DELETE FROM {self._table_sql()} WHERE id = "
                    f"{format_value(migration_id)};"
                )
        else:
            raise ValueError("direction must be 'up' or 'down'.")
        return "\n".join(lines)

    def down_to(self, target: str) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        """
        applied = self.applied()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return self.down(steps) if steps else []

    def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step and be registered with
        this migrator. Returns the ids that were reverted.
        """
        with self._lock_scope():
            applied = self.applied()
            by_id = {m.id: m for m in self._migrations}
            placeholder = self._compiler.placeholder()
            reverted: List[str] = []
            for migration_id in reversed(applied[-steps:] if steps else []):
                migration = by_id.get(migration_id)
                if migration is None:
                    raise ValueError(
                        f"Applied migration '{migration_id}' is not registered "
                        "with this migrator; cannot revert."
                    )
                if migration.down is None:
                    raise ValueError(f"Migration '{migration_id}' has no down step.")
                with self._migration_scope():
                    _run_step(self._connection, migration.down)
                    self._connection.cursor().execute(
                        f"DELETE FROM {self._table_sql()} WHERE id = {placeholder}",
                        (migration_id,),
                    )
                reverted.append(migration_id)
            return reverted
