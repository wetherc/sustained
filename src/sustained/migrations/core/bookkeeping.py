"""
The migrator's own tables and lock: creating and upgrading the tracking
table, reading its rows, the failed-attempt rows, validate(), repair(),
baseline() and script(), the rehearsal rows, and the advisory lock around
a run.

Each function here is the body of a migrator method, or a helper of one,
written once for both migrators. What a public method promises is on its
docstring in Migrator; the notes here are about how.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sustained.driver_errors import is_missing_table
from sustained.migrations.checks import (
    _checksum_repair,
    _failed_attempt_problem,
    _is_current,
    _migration_state,
    _validation_problems,
)
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    Commit,
    Core,
    Execute,
    ExecuteMany,
    Fetch,
    RefuseOpenTransaction,
    T,
    TakeLock,
    rollback_quietly,
)
from sustained.migrations.migration import (
    AppliedRecord,
    Migration,
    _restore_migration,
    _stored_steps,
    migration_checksum,
)
from sustained.migrations.planning import render_script
from sustained.migrations.rehearsal import (
    REHEARSAL_FAILED,
    REHEARSAL_OVERRIDE,
    REHEARSAL_PASSED,
    Rehearsal,
    _destructive_in,
    _legacy_rehearsal_key,
    _rehearsal_message,
    _rehearsal_writes,
    _scratch_rehearsal_keys,
    rehearsal_key,
)
from sustained.migrations.tracking import (
    _UPGRADE_COLUMNS,
    _lock_message,
    _next_seq,
    _rehearsal_column_defs,
    _tracking_column_defs,
    _unlock_message,
    _upgrade_column_def,
    quoted_columns,
    records_from_rows,
    records_select,
)
from sustained.types import RowValue


def lock_scope(m: MigratorBase, body: Core[T]) -> Core[T]:
    """
    Holds the engine's advisory lock, named after the tracking table,
    for the duration of the body, so concurrent migrators queue instead of
    racing. A no-op on engines without one.
    """
    from sustained.exceptions import MigrationError

    lock_statements = m._compiler.migration_lock_sql(m._table)
    if not lock_statements:
        return (yield from body)
    for statement in lock_statements:
        row: Optional[Sequence[RowValue]] = yield TakeLock(statement)
        # MySQL and MSSQL report a refused lock in the value they return
        # instead of raising, so a run that read nothing here could start
        # while another migrator was working. A statement that returns no
        # row reads as a lock that was not granted.
        problem = m._compiler.migration_lock_problem(row)
        if problem is not None:
            raise MigrationError([_lock_message(m._table, problem)])
    try:
        result = yield from body
    except BaseException:
        # A failed statement outside a transaction block leaves a
        # Postgres session aborted. The engine then refuses every
        # statement, pg_advisory_unlock included, until a rollback.
        yield from rollback_quietly()
        yield from release_lock(m, raising=False)
        raise
    yield from release_lock(m, raising=True)
    return result


def release_lock(m: MigratorBase, raising: bool) -> Core[None]:
    """
    Runs the unlock statements. A refused unlock leaves the lock held
    until the connection closes, and every other migrator waits for
    it until then. After a run that succeeded, the refusal raises.
    After a run that failed, it is reported on stderr, so the run's
    own error is the one the caller sees.
    """
    from sustained.exceptions import MigrationError

    failure: Optional[Exception] = None
    for statement in m._compiler.migration_unlock_sql(m._table):
        try:
            yield Execute(statement)
        except Exception as error:
            failure = failure or error
    if failure is None:
        return
    message = _unlock_message(m._table, failure)
    if not raising:
        print(f"error: {message}", file=sys.stderr)
        return
    raise MigrationError([message]) from failure


def ensure_tracking_table(m: MigratorBase) -> Core[None]:
    from sustained.schema import build_create_table_sql

    if m._tracking_ready:
        return
    sql = build_create_table_sql(
        m._compiler,
        m._table_ddl_sql(),
        _tracking_column_defs(m._compiler.supports_constraints()),
        if_not_exists=True,
        options=m._tracking_table_options,
    )
    yield Execute(sql)
    yield Commit()
    yield from upgrade_tracking_table(m)
    m._tracking_ready = True


def has_columns(m: MigratorBase, columns: Tuple[str, ...]) -> Core[bool]:
    """Probes the tracking table for the given columns."""
    try:
        yield Fetch(
            f"SELECT {quoted_columns(m._compiler, *columns)} "
            f"FROM {m._table_sql()} WHERE 1 = 0"
        )
        return True
    except Exception:
        # A failed probe can poison an open transaction (Postgres
        # aborts it), so clear the slate before the next statement.
        yield from rollback_quietly()
        return False


def upgrade_tracking_table(m: MigratorBase) -> Core[None]:
    """
    Brings a tracking table written by an earlier version, which held
    only id and applied_at, up to the current shape. Missing columns
    are added nullable; seq and success are backfilled from the
    existing rows in applied order.
    """
    from sustained.schema import render_column_sql

    if (yield from has_columns(m, _UPGRADE_COLUMNS)):
        return
    compiler = m._compiler
    added: List[str] = []
    for name in _UPGRADE_COLUMNS:
        if (yield from has_columns(m, (name,))):
            continue
        column_sql = render_column_sql(
            compiler, name, _upgrade_column_def(name), inline_pk=False
        )
        yield Execute(compiler.compile_add_column(m._table_ddl_sql(), column_sql))
        added.append(name)
    yield Commit()
    placeholder = compiler.placeholder()
    # Backfill only the columns this run added, and only where they are
    # still null, so values a partial earlier upgrade wrote survive.
    column = compiler.quote_identifier
    if "success" in added:
        yield Execute(
            f"UPDATE {m._table_sql()} SET {column('success')} = "
            f"{placeholder} WHERE {column('success')} IS NULL",
            (True,),
        )
    if "seq" in added:
        rows: List[Sequence[RowValue]] = yield Fetch(
            f"SELECT {column('id')} FROM {m._table_sql()} "
            f"ORDER BY {quoted_columns(compiler, 'applied_at', 'id')}"
        )
        for position, row in enumerate(rows, start=1):
            yield Execute(
                f"UPDATE {m._table_sql()} SET {column('seq')} = "
                f"{placeholder} WHERE {column('id')} = {placeholder} "
                f"AND {column('seq')} IS NULL",
                (position, row[0]),
            )
    yield Commit()


def read_records(m: MigratorBase) -> Core[List[AppliedRecord]]:
    """Reads the tracking table rows, assuming the table is there."""
    rows: List[Sequence[RowValue]] = yield Fetch(
        records_select(m._compiler, m._table_sql())
    )
    return records_from_rows(rows)


def applied_records(m: MigratorBase) -> Core[List[AppliedRecord]]:
    yield from ensure_tracking_table(m)
    return (yield from read_records(m))


def read_applied_records(m: MigratorBase) -> Core[List[AppliedRecord]]:
    """
    The tracking table rows, without writing anything. An empty list
    means the run has no history to read: either the tracking table does
    not exist yet, or it has only the columns an earlier version wrote.
    Any other failed read raises the driver's error.
    """
    if m._tracking_ready:
        return (yield from read_records(m))
    try:
        return (yield from read_records(m))
    except Exception as error:
        # A failed read can poison an open transaction, so clear the
        # slate before the next statement.
        yield from rollback_quietly()
        if is_missing_table(error) or (yield from has_earlier_columns(m)):
            return []
        raise


def has_earlier_columns(m: MigratorBase) -> Core[bool]:
    """True when the tracking table lacks the columns added later."""
    return (yield from has_columns(m, ("id",))) and not (
        yield from has_columns(m, _UPGRADE_COLUMNS)
    )


def applied(m: MigratorBase) -> Core[List[str]]:
    return [r.id for r in (yield from applied_records(m)) if r.success]


def read_applied(m: MigratorBase) -> Core[List[str]]:
    return [r.id for r in (yield from read_applied_records(m)) if r.success]


def pending(m: MigratorBase) -> Core[List[Migration]]:
    records = {r.id: r for r in (yield from read_applied_records(m))}
    result = [x for x in m._versioned() if not _is_current(records.get(x.id), x, False)]
    result.extend(
        x for x in m._repeatables() if not _is_current(records.get(x.id), x, True)
    )
    return result


def status(m: MigratorBase) -> Core[List[Tuple[str, bool]]]:
    applied_ids = set((yield from read_applied(m)))
    return [(x.id, x.id in applied_ids) for x in m._migrations]


def statuses(m: MigratorBase) -> Core[List[Tuple[str, str]]]:
    records = {r.id: r for r in (yield from read_applied_records(m))}
    return [(x.id, _migration_state(records.get(x.id), x)) for x in m._migrations]


def validate(m: MigratorBase, raise_on_problems: bool = True) -> Core[List[str]]:
    from sustained.exceptions import MigrationError

    problems = _validation_problems(m._migrations, (yield from read_applied_records(m)))
    if problems and raise_on_problems:
        raise MigrationError(problems)
    return problems


def repair(m: MigratorBase) -> Core[List[str]]:
    yield RefuseOpenTransaction("repair")
    records = yield from applied_records(m)
    compiler = m._compiler
    by_id = {x.id: x for x in m._migrations}
    placeholder = compiler.placeholder()
    actions: List[str] = []
    for record in records:
        if not record.success:
            yield Execute(
                f"DELETE FROM {m._table_sql()} WHERE "
                f"{compiler.quote_identifier('id')} = {placeholder} "
                f"AND {compiler.quote_identifier('success')} = "
                f"{compiler.compile_boolean(False)}",
                (record.id,),
            )
            actions.append(f"removed the failed attempt of '{record.id}'")
            continue
        migration = by_id.get(record.id)
        if migration is None:
            continue
        rewrite = _checksum_repair(record, migration)
        if rewrite is not None:
            current, action = rewrite
            yield Execute(
                f"UPDATE {m._table_sql()} SET "
                f"{compiler.quote_identifier('checksum')} = "
                f"{placeholder} WHERE "
                f"{compiler.quote_identifier('id')} = {placeholder}",
                (current, record.id),
            )
            actions.append(action)
    yield Commit()
    return actions


def baseline(m: MigratorBase, target: str) -> Core[List[str]]:
    from sustained.exceptions import MigrationError

    yield RefuseOpenTransaction("baseline")
    versioned = m._versioned()
    ids = [x.id for x in versioned]
    if target not in ids:
        if any(x.id == target for x in m._repeatables()):
            raise ValueError(
                f"Migration target {target!r} is repeatable; a target "
                "must name a versioned migration."
            )
        raise ValueError(f"Unknown migration target: {target!r}.")

    def locked() -> Core[List[str]]:
        records = yield from applied_records(m)
        already_applied = {r.id for r in records if r.success}
        candidates = versioned[: ids.index(target) + 1] + m._repeatables()
        # A failed row keeps the id, so a second row for it breaks the
        # table's primary key part way through the run.
        failed_ids = {r.id for r in records if not r.success}
        failed = [
            _failed_attempt_problem(x.id) for x in candidates if x.id in failed_ids
        ]
        if failed:
            raise MigrationError(failed)
        next_seq = _next_seq(records)
        recorded: List[str] = []
        try:
            for migration in candidates:
                if migration.id in already_applied:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                yield Execute(
                    m._insert_sql(),
                    (
                        migration.id,
                        next_seq,
                        migration_checksum(migration),
                        timestamp,
                        None,
                        True,
                        False,
                        None,
                    ),
                )
                next_seq += 1
                recorded.append(migration.id)
            yield Commit()
        except BaseException:
            # Without an advisory lock no scope rolls back, and rows
            # already inserted would be written by the next commit.
            yield from rollback_quietly()
            raise
        return recorded

    return (yield from lock_scope(m, locked()))


def record_failure(
    m: MigratorBase,
    migration: Migration,
    seq: int,
    update: bool = False,
    generated: bool = False,
) -> Core[None]:
    """
    Writes a failed-attempt row after a migration step raised on an
    engine whose schema changes do not roll back, where partial changes
    may remain. A repeatable that already has a row updates it in
    place. A failure to write the row never masks the original error. A
    rehearsal writes nothing: its whole run rolls back. A migration
    that asked for no transaction leaves partial changes on every
    engine, so it gets a row wherever it fails.
    """
    if m._rehearsing or (
        migration.transactional and m._compiler.supports_transactional_ddl()
    ):
        return
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        checksum = migration_checksum(migration)
        steps = _stored_steps(migration, generated, m._compiler)
        if update:
            yield Execute(
                m._update_sql(),
                (checksum, timestamp, None, False, generated, steps, migration.id),
            )
        else:
            yield Execute(
                m._insert_sql(),
                (migration.id, seq, checksum, timestamp, None, False, generated, steps),
            )
        yield Commit()
    except Exception:
        pass


def record_down_failure(m: MigratorBase, migration: Migration) -> Core[None]:
    """
    Marks a migration's row failed after its down step raised where
    nothing rolled the step back, so partial changes may remain. The
    row keeps its checksum and stored steps. A failure to write the
    mark never masks the original error, and a transaction that rolled
    the step back leaves the row as it was.
    """
    if m._rehearsing or (
        migration.transactional and m._compiler.supports_transactional_ddl()
    ):
        return
    column = m._compiler.quote_identifier
    placeholder = m._compiler.placeholder()
    try:
        yield Execute(
            f"UPDATE {m._table_sql()} SET {column('success')} = "
            f"{placeholder} WHERE {column('id')} = {placeholder}",
            (False, migration.id),
        )
        yield Commit()
    except Exception:
        pass


def generated_migration(
    m: MigratorBase, migration_id: str
) -> Core[Optional[Migration]]:
    """
    The migration a generated tracking row describes, read back from
    the row itself, or None when the row holds no statements.

    up(models=[...]) applies a migration that exists nowhere but that
    run, so a later process has nothing to revert it with. The row
    carries the statements for exactly that reason, and either migrator
    can take the migration back.
    """
    compiler = m._compiler
    rows: List[Sequence[RowValue]] = yield Fetch(
        f"SELECT {compiler.quote_identifier('steps')} "
        f"FROM {m._table_sql()} WHERE "
        f"{compiler.quote_identifier('id')} = {compiler.placeholder()}",
        (migration_id,),
    )
    if not rows or rows[0][0] is None:
        return None
    return _restore_migration(migration_id, str(rows[0][0]))


def script(m: MigratorBase, direction: str = "up") -> Core[str]:
    records = yield from read_applied_records(m)
    generated: Dict[str, Migration] = {}
    if direction == "down":
        registered = {x.id for x in m._migrations}
        for record in records:
            if record.generated and record.id not in registered:
                restored = yield from generated_migration(m, record.id)
                if restored is not None:
                    generated[record.id] = restored
    return render_script(
        m._compiler,
        m._table_sql(),
        m._migrations,
        records,
        direction,
        generated,
    )


def ensure_rehearsal_table(m: MigratorBase) -> Core[None]:
    from sustained.schema import build_create_table_sql

    if m._rehearsal_ready:
        return
    sql = build_create_table_sql(
        m._compiler,
        m._rehearsal_table_ddl_sql(),
        _rehearsal_column_defs(m._compiler.supports_constraints()),
        if_not_exists=True,
        options=m._tracking_table_options,
    )
    yield Execute(sql)
    yield Commit()
    m._rehearsal_ready = True


def record_rehearsal(
    m: MigratorBase, key: str, outcome: str = REHEARSAL_PASSED
) -> Core[None]:
    if outcome not in (REHEARSAL_PASSED, REHEARSAL_FAILED, REHEARSAL_OVERRIDE):
        raise ValueError(
            f"Unknown rehearsal outcome {outcome!r}; use "
            f"{REHEARSAL_PASSED!r}, {REHEARSAL_FAILED!r}, or "
            f"{REHEARSAL_OVERRIDE!r}."
        )
    yield RefuseOpenTransaction("record_rehearsal")
    yield from record_rehearsals(m, [key], outcome)


def record_rehearsals(
    m: MigratorBase, keys: Sequence[str], outcome: str = REHEARSAL_PASSED
) -> Core[None]:
    """
    Writes one row per key with the same outcome, in one transaction.
    A rehearsal of n pending migrations can prove n * (n + 1) / 2
    prefix keys. For 400 destructive migrations on a local SQLite
    file, a commit per key takes 25 s for the 80,200 rows, where one
    transaction takes 0.4 s.
    """
    yield from ensure_rehearsal_table(m)
    delete_sql, insert_sql, rows = _rehearsal_writes(
        m._compiler, m._rehearsal_table_sql(), keys, outcome
    )
    yield ExecuteMany(((delete_sql, [row[:1] for row in rows]), (insert_sql, rows)))
    yield Commit()


def record_scratch_rehearsal(
    m: MigratorBase, results: Rehearsal
) -> Core[Optional[str]]:
    keys = _scratch_rehearsal_keys(
        (yield from applied_records(m)),
        (yield from pending(m)),
        results,
        m._compiler,
    )
    if not keys:
        return None
    yield RefuseOpenTransaction("record_scratch_rehearsal")
    yield from record_rehearsals(m, keys)
    return keys[0]


def rehearsal_outcome(m: MigratorBase, key: str) -> Core[Optional[str]]:
    yield from ensure_rehearsal_table(m)
    placeholder = m._compiler.placeholder()
    rows: List[Sequence[RowValue]] = yield Fetch(
        f"SELECT outcome FROM {m._rehearsal_table_sql()} "
        f"WHERE rehearsal_key = {placeholder}",
        (key,),
    )
    return None if not rows else str(rows[0][0])


def rehearsed(m: MigratorBase, key: str) -> Core[bool]:
    return (yield from rehearsal_outcome(m, key)) == REHEARSAL_PASSED


def run_outcome(
    m: MigratorBase, applied: Sequence[AppliedRecord], run: Sequence[Migration]
) -> Core[Optional[str]]:
    outcome = yield from rehearsal_outcome(m, rehearsal_key(applied, run))
    legacy = _legacy_rehearsal_key(applied, run)
    if outcome is None and legacy is not None:
        outcome = yield from rehearsal_outcome(m, legacy)
    return outcome


def require_rehearsal_row(
    m: MigratorBase,
    records: List[AppliedRecord],
    run: List[Migration],
    unrehearsed: bool,
    target: Optional[str] = None,
) -> Core[None]:
    """
    Stops a run that removes data unless a passing rehearsal covers
    exactly this content. A run that only adds passes straight
    through and never reads the rehearsal table.
    """
    from sustained.exceptions import RehearsalRequired

    if unrehearsed:
        return
    destructive = _destructive_in(run, m._compiler)
    if not destructive:
        return
    outcome = yield from run_outcome(m, records, run)
    if outcome == REHEARSAL_PASSED:
        return
    raise RehearsalRequired(_rehearsal_message(destructive, outcome, target))
