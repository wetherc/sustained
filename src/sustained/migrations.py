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
sustained.autogenerate and Migrator.up(models=[...]).
"""

from __future__ import annotations

import hashlib
import time
import warnings
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
    cast,
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

    A repeatable migration re-runs whenever its checksum changes, for
    views, functions, and seed data. Repeatables have no down step and
    run after every versioned migration.
    """

    def __init__(
        self,
        id: str,
        up: MigrationStep,
        down: Optional[MigrationStep] = None,
        checksum: Optional[str] = None,
        repeatable: bool = False,
    ) -> None:
        if not id:
            raise ValueError("A migration needs a non-empty id.")
        if repeatable and down is not None:
            raise ValueError(
                f"Repeatable migration '{id}' cannot have a down step; "
                "repeatables re-run instead of reverting."
            )
        if repeatable and callable(up) and checksum is None:
            raise ValueError(
                f"Repeatable migration '{id}' has a callable step; pass an "
                "explicit checksum so re-runs can be detected."
            )
        self.id = id
        self.up = up
        self.down = down
        self.checksum = checksum
        self.repeatable = repeatable


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
    """
    One row of the tracking table. `generated` marks a migration written
    by the diff against the models rather than registered by hand, which
    is why nothing on disk carries its id.
    """

    id: str
    seq: Optional[int]
    checksum: Optional[str]
    success: bool
    generated: bool = False


class RehearsalResult(NamedTuple):
    """
    What a rehearsal proved about one migration.

    `up_ok` reports whether the up step ran. `down_ok` reports whether the
    down step ran, or None when nothing was proved, and `error` then says
    why: the migration has no down step, the sweep never reached it, or it
    is a repeatable, which has no down step to prove. When a step raised,
    `error` holds the database error.

    `landed` and `reversed` carry what the schema itself said. Each is
    None when it was not checked, an empty list when it was checked and
    proved, and a list of readable lines when it failed. `landed` is only
    checked for the migration generated from the models, since a
    hand-written migration may create objects no model declares.
    `reversed` compares the schema after the down sweep against the
    snapshot taken before the rehearsal, so it is shared by every
    migration in the run: a leftover object names the whole sweep, not
    one step of it.
    """

    id: str
    up_ok: bool
    down_ok: Optional[bool]
    error: Optional[str]
    landed: Optional[List[str]] = None
    reversed: Optional[List[str]] = None


# Dialects whose schema changes roll back, so a rehearsal can undo itself.
# The others need a scratch database; see Migrator.rehearse(). DEFAULT is
# on the list for SQLite, the engine the generic compiler usually serves;
# a config that leaves the dialect unset while pointing at an engine
# without transactional DDL, such as MySQL, should declare its dialect or
# rehearse against a scratch database.
_REHEARSABLE = frozenset({Dialects.DEFAULT, Dialects.POSTGRES, Dialects.DUCKDB})


def _check_rehearsable(dialect: Dialects) -> None:
    """
    Refuses to rehearse where a rollback would not take the schema back.
    """
    if dialect in _REHEARSABLE:
        return
    raise ValueError(
        f"rehearse needs a database whose schema changes roll back, and "
        f"{dialect.name.lower()} is not on that list "
        f"({', '.join(sorted(d.name.lower() for d in _REHEARSABLE))}). "
        "Point rehearse at a scratch database instead: pass scratch=True "
        "on a throwaway connection, or define get_rehearsal_connection() "
        "in the config module when running the CLI."
    )


# Columns added when upgrading a tracking table written by an earlier
# version, which held only id and applied_at.
_UPGRADE_COLUMNS = ("seq", "checksum", "execution_ms", "success", "generated")

_RECORDS_SELECT = "SELECT id, seq, checksum, success, generated FROM"


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
        }
    return {
        "id": String(255),
        "seq": Integer(),
        "checksum": String(64),
        "applied_at": Text(),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
    }


def _upgrade_column_def(name: str) -> "ColumnDef":
    """A nullable definition for one upgrade column, safe to ADD COLUMN."""
    from sustained.schema import Boolean, Integer, String

    defs: Dict[str, "ColumnDef"] = {
        "seq": Integer(),
        "checksum": String(64),
        "execution_ms": Integer(),
        "success": Boolean(),
        "generated": Boolean(),
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


def _is_current(
    record: Optional[AppliedRecord],
    current_checksum: Optional[str],
    repeatable: bool,
) -> bool:
    """True when the tracking row makes a run unnecessary."""
    if record is None or not record.success:
        return False
    if not repeatable:
        return True
    return record.checksum == current_checksum


def _migration_state(record: Optional[AppliedRecord], migration: Migration) -> str:
    """One migration's state: 'applied', 'pending', or 'changed'."""
    if record is None or not record.success:
        return "pending"
    if migration.repeatable and record.checksum != migration_checksum(migration):
        return "changed"
    return "applied"


def _down_sweep(ran: List[Migration]) -> Iterator[Tuple[Migration, Optional[str]]]:
    """
    The order a rehearsal runs its down steps in, newest first, paired with
    the reason a migration cannot be proved, or None when its down step
    should run.

    A repeatable has no down step and never blocks the sweep. A versioned
    migration without one does block it: everything older sits under
    changes that cannot be taken back, so their down steps cannot run
    either.
    """
    blocked: Optional[str] = None
    for migration in reversed(ran):
        if migration.repeatable:
            yield migration, "no down step (repeatable)"
        elif blocked is not None:
            yield migration, f"down not reached: '{blocked}' has no down step"
        elif migration.down is None:
            blocked = migration.id
            yield migration, "no down step"
        else:
            yield migration, None


def _rehearsal_results(
    ran: List[Migration],
    up_error: Optional[Tuple[str, str]],
    down_outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]],
    landed: Optional[Dict[str, List[str]]] = None,
    reverted: Optional[List[str]] = None,
) -> List[RehearsalResult]:
    """
    Merges the up and down outcomes into one result per migration, in the
    order the up steps ran. `up_error` is the (id, message) pair of the
    migration that stopped the rehearsal, if one did.

    `landed` holds the outstanding differences per migration id, for the
    migrations whose landing was checked. `reverted` holds the schema
    left over after the down sweep, and goes on every migration whose
    down step ran, since one sweep proves them together.
    """
    landed = landed or {}
    unfinished: Tuple[Optional[bool], Optional[str]] = (
        None,
        "down not rehearsed: the run stopped",
    )
    results = [
        RehearsalResult(
            m.id,
            True,
            *down_outcomes.get(m.id, unfinished),
            landed=landed.get(m.id),
            reversed=(
                reverted if down_outcomes.get(m.id, unfinished)[0] is True else None
            ),
        )
        for m in ran
    ]
    if up_error is not None:
        results.append(RehearsalResult(up_error[0], False, None, up_error[1]))
    return results


def _tag_migration(error: BaseException, migration_id: str) -> None:
    """
    Records which migration raised on the exception itself, so a caller
    that catches it can name the migration. The CLI reads it when it hands
    a failure to the config module's on_error callback, and when it prints
    the error. An exception type that rejects new attributes, such as one
    with __slots__, keeps its error unmarked rather than masking it.
    """
    try:
        setattr(error, "migration_id", migration_id)
    except Exception:
        pass


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
    require_registered=False skips the unknown-id check.

    A row marked generated is never reported as unknown. It was written
    by a diff against the models, so no file or list carries its id, and
    a later run regenerates whatever difference is left.
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
            if require_registered and not record.generated:
                problems.append(
                    f"applied migration '{record.id}' is not registered "
                    "with this migrator"
                )
            continue
        if migration.repeatable:
            # A changed checksum is the re-run signal, not a problem.
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
            if migration.repeatable:
                continue
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
        self._rehearsing = False

    @property
    def connection(self) -> Any:
        """The connection this migrator runs on."""
        return self._connection

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    @contextmanager
    def _migration_scope(self) -> Iterator[None]:
        """
        A transaction on engines that have them; a bare run followed by a
        commit (when the driver has one) on engines that do not.

        A rehearsal opens one transaction around the whole run and rolls it
        back at the end, so each migration runs bare and nothing commits.
        """
        if self._rehearsing:
            yield
            return
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
        placeholder = self._compiler.placeholder()
        cursor = self._connection.cursor()
        # Backfill only the columns this run added, and only where they are
        # still null, so values a partial earlier upgrade wrote survive.
        if "success" in added:
            cursor.execute(
                f"UPDATE {self._table_sql()} SET success = {placeholder} "
                "WHERE success IS NULL",
                (True,),
            )
        if "seq" in added:
            cursor.execute(
                f"SELECT id FROM {self._table_sql()} ORDER BY applied_at, id"
            )
            ids = [row[0] for row in cursor.fetchall()]
            for position, migration_id in enumerate(ids, start=1):
                cursor.execute(
                    f"UPDATE {self._table_sql()} SET seq = {placeholder} "
                    f"WHERE id = {placeholder} AND seq IS NULL",
                    (position, migration_id),
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
            AppliedRecord(row[0], row[1], row[2], bool(row[3]), bool(row[4]))
            for row in cursor.fetchall()
        ]

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in self.applied_records() if r.success]

    def _versioned(self) -> List[Migration]:
        return [m for m in self._migrations if not m.repeatable]

    def _repeatables(self) -> List[Migration]:
        return [m for m in self._migrations if m.repeatable]

    def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        records = {r.id: r for r in self.applied_records()}
        result = [
            m
            for m in self._versioned()
            if not _is_current(records.get(m.id), migration_checksum(m), False)
        ]
        result.extend(
            m
            for m in self._repeatables()
            if not _is_current(records.get(m.id), migration_checksum(m), True)
        )
        return result

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def statuses(self) -> List[tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        records = {r.id: r for r in self.applied_records()}
        return [
            (m.id, _migration_state(records.get(m.id), m)) for m in self._migrations
        ]

    def _insert_sql(self) -> str:
        placeholder = self._compiler.placeholder()
        values = ", ".join([placeholder] * 7)
        return (
            f"INSERT INTO {self._table_sql()} "
            f"(id, seq, checksum, applied_at, execution_ms, success, generated) "
            f"VALUES ({values})"
        )

    def _update_sql(self) -> str:
        placeholder = self._compiler.placeholder()
        return (
            f"UPDATE {self._table_sql()} "
            f"SET checksum = {placeholder}, applied_at = {placeholder}, "
            f"execution_ms = {placeholder}, success = {placeholder}, "
            f"generated = {placeholder} "
            f"WHERE id = {placeholder}"
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

        Repeatables keep their stored checksums. For them a changed
        checksum schedules a re-run, and rewriting the row here would
        cancel that run without the new contents ever reaching the
        database.
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
            if migration is None or migration.repeatable:
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

    def _record_failure(
        self,
        migration: Migration,
        seq: int,
        update: bool = False,
        generated: bool = False,
    ) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine without transactions, where partial changes may remain. A
        repeatable that already has a row updates it in place. A failure
        to write the row never masks the original error. A rehearsal writes
        nothing: its whole run rolls back.
        """
        if self._rehearsing or self._compiler.supports_transactions():
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            checksum = migration_checksum(migration)
            cursor = self._connection.cursor()
            if update:
                cursor.execute(
                    self._update_sql(),
                    (checksum, timestamp, None, False, generated, migration.id),
                )
            else:
                cursor.execute(
                    self._insert_sql(),
                    (migration.id, seq, checksum, timestamp, None, False, generated),
                )
            self._commit_quietly()
        except Exception:
            pass

    def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.

        The run validates first: failed attempts, unknown applied ids,
        checksum mismatches, and out-of-order pending migrations all stop
        it. Pass validate=False to skip the checks, or
        allow_out_of_order=True to accept a pending migration that is
        ordered before an applied one.

        Repeatables run after the versioned migrations, whenever their
        checksum is new or changed. A targeted run skips them: a
        repeatable may depend on a versioned migration past the target,
        and the next full up() runs it. The target must name a versioned
        migration.

        With models, the run applies the versioned migrations first, then
        diffs the models against the database and applies the generated
        migration, then the repeatables. The diff is taken after the
        pending migrations have run, so it sees the schema they left and
        never regenerates a table one of them just created. Additive
        changes generate reversible steps, so down() takes them back.
        Drops need allow_drops=True and do not reverse. A generated
        migration always runs last of the versioned ones, so it cannot be
        combined with a target, and the remaining arguments are the diff
        options plan() takes.
        """
        from sustained.exceptions import MigrationError

        if models is not None and target is not None:
            raise ValueError(
                "up() cannot take both models and a target: the generated "
                "migration always runs last, so a target would leave it out."
            )
        require_registered = models is None

        with self._lock_scope():
            if models is not None:
                self._ensure_tracking_table()
                # A generated id carries the moment it was generated, which
                # sorts after every applied migration but not necessarily
                # after every registered one.
                allow_out_of_order = True

            migrations = self._versioned()
            if target is not None:
                ids = [m.id for m in migrations]
                if target not in ids:
                    if any(m.id == target for m in self._repeatables()):
                        raise ValueError(
                            f"Migration target {target!r} is repeatable; a "
                            "target must name a versioned migration."
                        )
                    raise ValueError(f"Unknown migration target: {target!r}.")
                migrations = migrations[: ids.index(target) + 1]

            records = self.applied_records()
            if validate:
                problems = _validation_problems(
                    self._migrations,
                    records,
                    allow_out_of_order,
                    require_registered=require_registered,
                )
                if problems:
                    raise MigrationError(problems)
            records_by_id = {r.id: r for r in records}
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            applied_now: List[str] = []
            for migration in migrations:
                if migration.id in already_applied:
                    continue
                self._apply(migration, next_seq, update=False)
                next_seq += 1
                applied_now.append(migration.id)
            if models is not None:
                generated = self.plan(
                    models,
                    allow_drops=allow_drops,
                    ignore_changed_columns=ignore_changed_columns,
                    migration_id=migration_id,
                    renames=renames,
                    table_renames=table_renames,
                    type_casts=type_casts,
                )
                if generated is not None:
                    self._migrations.append(generated)
                    self._apply(generated, next_seq, update=False, generated=True)
                    next_seq += 1
                    applied_now.append(generated.id)
            for migration in self._repeatables() if target is None else []:
                record = records_by_id.get(migration.id)
                if _is_current(record, migration_checksum(migration), True):
                    continue
                self._apply(migration, next_seq, update=record is not None)
                if record is None:
                    next_seq += 1
                applied_now.append(migration.id)
            return applied_now

    def _apply(
        self, migration: Migration, seq: int, update: bool, generated: bool = False
    ) -> None:
        """
        Runs one migration's up step and records it: an INSERT for a
        first run, an UPDATE in place when a repeatable re-runs, keeping
        its original seq. `generated` marks a migration the diff against
        the models produced, whose id nothing on disk carries.
        """
        try:
            with self._migration_scope():
                started = time.perf_counter()
                _run_step(self._connection, migration.up)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                timestamp = datetime.now(timezone.utc).isoformat()
                checksum = migration_checksum(migration)
                cursor = self._connection.cursor()
                if update:
                    cursor.execute(
                        self._update_sql(),
                        (
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            migration.id,
                        ),
                    )
                else:
                    cursor.execute(
                        self._insert_sql(),
                        (
                            migration.id,
                            seq,
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                        ),
                    )
        except Exception as error:
            self._record_failure(migration, seq, update=update, generated=generated)
            _tag_migration(error, migration.id)
            raise

    def rehearse(
        self,
        scratch: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
    ) -> List[RehearsalResult]:
        """
        Runs every pending migration up, then back down, inside one
        transaction, and rolls that transaction back. Returns one result
        per migration that ran; an empty list means nothing was pending.

        A rehearsal proves that the SQL is valid, that the schema moved,
        and that the down steps take it back. It does not prove anything
        about data on a production-sized table. The tracking table is
        created if it does not exist yet; nothing else survives. A
        callable step that commits on its own is the exception: that
        commit cannot be taken back.

        With models, the run rehearses what up(models=[...]) would apply:
        the generated migration joins the pending list for this run only,
        and its result reports whether the schema then matched the models.
        The remaining arguments are the diff options up() takes, and they
        should match the ones the real run will use.

        The up steps run in order. Repeatables run after them, as in up().
        The down steps then run newest-first, skipping the repeatables,
        which have none. The first migration without a down step stops the
        sweep, since everything older sits under changes that cannot be
        taken back. The first step that raises stops the rehearsal.

        The schema is read before the run and again after the down sweep.
        A difference between the two means a down step ran without taking
        its change back. Tables and columns are compared; indexes,
        constraints, and defaults are not, so a leftover index is not
        reported yet.

        Only dialects whose schema changes roll back may rehearse. Pass
        scratch=True when the connection points at a database that can be
        thrown away: the dialect check is then skipped, and the changes may
        survive the rollback.
        """
        from sustained.execution import in_transaction

        if not scratch:
            _check_rehearsable(self._dialect)
        if getattr(self._connection, "autocommit", False) is True:
            raise ValueError(
                "rehearse cannot run on a connection in autocommit mode: "
                "nothing would roll back. Open the connection without "
                "autocommit, or point rehearse at a scratch database."
            )
        if in_transaction(self._connection):
            raise ValueError(
                "rehearse cannot run inside an open transaction() block: "
                "its rollback would take the caller's work back too."
            )
        # The lock sits outside the rehearsal transaction, so the rollback
        # runs before the lock is released. The state reads sit inside it,
        # so a concurrent migrator cannot apply between the read and the
        # rehearsal.
        with self._lock_scope():
            self.validate()
            pending = self.pending()
            if not pending and models is None:
                return []
            if models is not None:
                self._ensure_tracking_table()
            records = {r.id: r for r in self.applied_records()}
            seq = _next_seq(list(records.values()))
            before = self._snapshot()
            self._rehearsing = True
            try:
                # Close whatever transaction the reads above opened, so the
                # explicit BEGIN starts a fresh one instead of warning.
                self._rollback_quietly()
                begin = self._compiler.begin_transaction_sql()
                if begin is not None:
                    self._connection.cursor().execute(begin)
                ran: List[Migration] = []
                up_error: Optional[Tuple[str, str]] = None
                for migration in pending:
                    try:
                        self._apply(migration, seq, update=migration.id in records)
                    except Exception as error:
                        up_error = (migration.id, str(error))
                        break
                    seq += 1
                    ran.append(migration)
                landed: Dict[str, List[str]] = {}
                if models is not None and up_error is None:
                    # The diff is taken here, inside the rehearsal, so it
                    # sees the schema the pending migrations just left. The
                    # generated migration joins the run without being
                    # registered: nothing outside the rehearsal should see a
                    # migration the rollback is about to take back.
                    drift = self.plan(
                        models,
                        allow_drops=allow_drops,
                        ignore_changed_columns=ignore_changed_columns,
                        migration_id=migration_id,
                        renames=renames,
                        table_renames=table_renames,
                        type_casts=type_casts,
                    )
                    if drift is not None:
                        try:
                            self._apply(drift, seq, update=False, generated=True)
                        except Exception as error:
                            up_error = (drift.id, str(error))
                        else:
                            seq += 1
                            ran.append(drift)
                            landed[drift.id] = self.drift(
                                models,
                                renames=renames,
                                table_renames=table_renames,
                            )
                outcomes = {} if up_error else self._rehearse_down(ran)
                reverted = None
                if before is not None and any(
                    down_ok for down_ok, _ in outcomes.values()
                ):
                    from sustained.autogenerate import diff_snapshots

                    after = self._snapshot()
                    if after is not None:
                        reverted = diff_snapshots(before, after)
                return _rehearsal_results(ran, up_error, outcomes, landed, reverted)
            finally:
                self._rehearsing = False
                self._roll_back_rehearsal()

    def _snapshot(self) -> Optional[Dict[str, Any]]:
        """
        The live schema, without the tracking table, or None when the
        database will not report it. A rehearsal compares two of these,
        and the tracking table is created by the rehearsal itself, so
        leaving it in would report it as an object left behind.

        A read that raises leaves the rehearsal's other proofs standing
        and reports the comparison as not checked, which is what a
        scratch database on an engine Sustained cannot introspect gives.
        """
        from sustained.autogenerate import introspect_schema

        try:
            schema = introspect_schema(self._connection, self._dialect)
        except Exception:
            return None
        schema.pop(self._table.lower(), None)
        return dict(schema)

    def drift(
        self,
        models: List[Type["Model"]],
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
    ) -> List[str]:
        """
        What the models still ask for, one readable line each, empty when
        the database holds everything they declare.

        Objects the database holds and the models do not are left out. A
        generated migration leaves those alone unless drops are allowed,
        so a schema built partly by hand does not read as drift here. Use
        plan() for the full comparison, drops included.
        """
        from sustained.autogenerate import diff_schema

        diff = diff_schema(
            self._connection,
            models,
            dialect=self._dialect,
            exclude_tables=(self._table,),
            renames=renames,
            table_renames=table_renames,
        )
        return diff.outstanding()

    def _roll_back_rehearsal(self) -> None:
        """
        Takes back everything the rehearsal did. The statement runs first,
        because a driver's own rollback() call does nothing on connections
        that never opened a transaction of their own; the driver call
        follows to leave its bookkeeping straight.
        """
        statement = self._compiler.rollback_transaction_sql()
        if statement is not None:
            try:
                self._connection.cursor().execute(statement)
            except Exception:
                pass
        self._rollback_quietly()

    def _rehearse_down(
        self, ran: List[Migration]
    ) -> Dict[str, Tuple[Optional[bool], Optional[str]]]:
        """
        Runs the down steps of a rehearsal, newest-first, and reports what
        each one proved. A step that raises stops the sweep; the
        migrations under it report that they were not reached.
        """
        placeholder = self._compiler.placeholder()
        outcomes: Dict[str, Tuple[Optional[bool], Optional[str]]] = {}
        failed: Optional[str] = None
        for migration, reason in _down_sweep(ran):
            if failed is not None:
                outcomes[migration.id] = (
                    None,
                    f"down not reached: '{failed}' down failed",
                )
            elif reason is not None:
                outcomes[migration.id] = (None, reason)
            else:
                try:
                    with self._migration_scope():
                        _run_step(self._connection, cast(MigrationStep, migration.down))
                        self._connection.cursor().execute(
                            f"DELETE FROM {self._table_sql()} WHERE id = {placeholder}",
                            (migration.id,),
                        )
                except Exception as error:
                    outcomes[migration.id] = (False, str(error))
                    failed = migration.id
                else:
                    outcomes[migration.id] = (True, None)
        return outcomes

    def baseline(self, target: str) -> List[str]:
        """
        Marks registered migrations up to and including the target as
        applied without running them, for adopting a database whose schema
        already matches. Rows are written with real checksums and a null
        execution time; already-applied migrations are skipped. Returns the
        ids that were recorded.

        The target must name a versioned migration. Every repeatable is
        recorded at its current checksum, so the first migrate after
        adoption does not re-run objects the schema already holds.
        """
        versioned = self._versioned()
        ids = [m.id for m in versioned]
        if target not in ids:
            if any(m.id == target for m in self._repeatables()):
                raise ValueError(
                    f"Migration target {target!r} is repeatable; a target "
                    "must name a versioned migration."
                )
            raise ValueError(f"Unknown migration target: {target!r}.")
        with self._lock_scope():
            records = self.applied_records()
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            cursor = self._connection.cursor()
            recorded: List[str] = []
            for migration in versioned[: ids.index(target) + 1] + self._repeatables():
                if migration.id in already_applied:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                cursor.execute(
                    self._insert_sql(),
                    (
                        migration.id,
                        next_seq,
                        migration_checksum(migration),
                        timestamp,
                        None,
                        True,
                        False,
                    ),
                )
                next_seq += 1
                recorded.append(migration.id)
            self._commit_quietly()
            return recorded

    def plan(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        ignore_undeclared: bool = True,
    ) -> Optional[Migration]:
        """
        Diffs the database against the models and returns the migration
        up(models=[...]) would generate, without registering or applying
        it. Returns None when the schema is already up to date. The
        tracking table is excluded from the diff.

        Objects the models do not declare are left alone, since a
        database may hold tables that hand-written migrations created.
        Pass allow_drops=True to generate the drops instead, or
        ignore_undeclared=False to refuse to generate while they exist.
        """
        from sustained.autogenerate import autogenerate

        generated_id = migration_id or datetime.now(timezone.utc).strftime(
            "auto_%Y%m%d%H%M%S_%f"
        )
        return autogenerate(
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
            ignore_undeclared=ignore_undeclared,
        )

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
        Deprecated since 2.13.0, removed in 3.0: call
        up(models=[...]) instead, which does the same work under the verb
        the CLI and the docs already use.
        """
        warnings.warn(
            "Migrator.sync() is deprecated and will be removed in 3.0. "
            "Call up(models=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.up(
            models=models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
        )

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
            records_by_id = {r.id: r for r in records}
            applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            for migration in self._versioned():
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
            for migration in self._repeatables():
                record = records_by_id.get(migration.id)
                checksum = migration_checksum(migration)
                if _is_current(record, checksum, True):
                    continue
                lines.append(f"-- repeat: {migration.id}")
                lines.extend(f"{s};" for s in migration_sql(migration, "up"))
                if record is None:
                    lines.append(
                        f"INSERT INTO {self._table_sql()} "
                        f"(id, seq, checksum, applied_at, execution_ms, success) "
                        f"VALUES ({format_value(migration.id)}, {next_seq}, "
                        f"{format_value(checksum)}, "
                        f"{format_value(placeholder_free_ts)}, NULL, "
                        f"{self._compiler.compile_boolean(True)});"
                    )
                    next_seq += 1
                else:
                    lines.append(
                        f"UPDATE {self._table_sql()} "
                        f"SET checksum = {format_value(checksum)}, "
                        f"applied_at = {format_value(placeholder_free_ts)}, "
                        f"execution_ms = NULL, "
                        f"success = {self._compiler.compile_boolean(True)} "
                        f"WHERE id = {format_value(migration.id)};"
                    )
        elif direction == "down":
            by_id = {m.id: m for m in self._migrations}
            for migration_id in reversed(self._applied_versioned()):
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

    def _applied_versioned(self) -> List[str]:
        """Applied ids with the repeatables left out; down() skips them."""
        repeatable_ids = {m.id for m in self._repeatables()}
        return [i for i in self.applied() if i not in repeatable_ids]

    def down_to(self, target: str) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted.
        """
        applied = self._applied_versioned()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return self.down(steps) if steps else []

    def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step and be registered with
        this migrator. Repeatables are never reverted. Returns the ids
        that were reverted.
        """
        with self._lock_scope():
            applied = self._applied_versioned()
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
