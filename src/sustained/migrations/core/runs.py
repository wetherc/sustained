"""
The runs themselves: up() and down() with the callbacks around them, one
migration applied or reverted inside its scope, and the diff against the
models that up(models=[...]) applies.

Each function here is the body of a migrator method, or a helper of one,
written once for both migrators. What a public method promises is on its
docstring in Migrator; the notes here are about how.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Type

from sustained.migrations.checks import (
    _changed_down_message,
    _changed_since_applied,
    _failed_attempt_problem,
    _is_current,
    _validation_problems,
    check_guards,
)
from sustained.migrations.core import bookkeeping
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    Autocommit,
    Commit,
    Core,
    DiffSource,
    Execute,
    Fire,
    RefuseOpenTransaction,
    RunStep,
    T,
    Transaction,
    run_in,
)
from sustained.migrations.migration import (
    Migration,
    MigrationStep,
    _checked_steps,
    _stored_steps,
    _tag_applied,
    _tag_migration,
    migration_checksum,
)
from sustained.migrations.planning import drift_lines, plan_migration
from sustained.migrations.rehearsal import (
    REHEARSAL_OVERRIDE,
    _destructive_in,
    rehearsal_key,
)
from sustained.migrations.tracking import _next_seq
from sustained.types import Connection

if TYPE_CHECKING:
    from sustained.guards import Verdict
    from sustained.introspect import Snapshot
    from sustained.model import Model


def migration_scope(
    m: MigratorBase, body: Core[T], transactional: bool = True
) -> Core[T]:
    """
    Runs one migration's body: inside a transaction on engines whose
    schema changes roll back; bare and followed by a commit on engines
    whose do not.

    `transactional` is the migration's own flag. A migration with
    transactional=False runs outside a transaction on every engine, so
    a statement the engine refuses inside a transaction block, such as
    CREATE INDEX CONCURRENTLY on Postgres, can run. Its tracking row is
    written after its statements, in the same bare mode, so a finished
    migration is still recorded. An async adapter over a driver that
    opens its own transaction, such as DbApiAsyncAdapter over psycopg2,
    is the limit here: the driver still opens one, and such a statement
    still fails. Run it on an adapter that executes bare, such as
    AsyncpgAdapter.

    A rehearsal opens one transaction around the whole run and rolls it
    back at the end, so each migration runs bare and nothing commits.

    Nothing takes a failed non-transactional migration back. The
    statements that already ran stay in the database, and the tracking
    row says the attempt failed. The operator finishes or undoes the
    rest by hand and then runs repair(). On Postgres a failed CREATE
    INDEX CONCURRENTLY also leaves an invalid index, which needs a
    DROP INDEX of its own.
    """
    if m._rehearsing:
        return (yield from body)
    if transactional and m._compiler.supports_transactional_ddl():
        return (yield from run_in(Transaction, body))
    if not transactional:
        return (yield from run_in(Autocommit, body))
    result = yield from body
    yield Commit()
    return result


def fire_on_error(m: MigratorBase, error: BaseException) -> Core[None]:
    """
    Hands a failed run to the on_error callback. A callback that raises
    must not replace the error it was told about, so its own failure is
    reported on stderr and set aside. before_migrate and after_migrate
    are called plainly: a failure there is the operator's own and stops
    the run.
    """
    hook = m._callbacks.on_error
    if hook is None:
        return
    try:
        yield Fire(hook, (getattr(error, "migration_id", None), error))
    except Exception as callback_error:
        print(f"error: on_error raised {callback_error!r}", file=sys.stderr)


def up(
    m: MigratorBase,
    target: Optional[str],
    validate: bool,
    allow_out_of_order: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    unrehearsed: bool,
) -> Core[List[str]]:
    yield RefuseOpenTransaction("up")
    callbacks = m._callbacks
    if callbacks.before_migrate is not None:
        yield Fire(callbacks.before_migrate)
    try:
        applied = yield from run_up(
            m,
            target=target,
            validate=validate,
            allow_out_of_order=allow_out_of_order,
            models=models,
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            unrehearsed=unrehearsed,
        )
    except Exception as error:
        yield from fire_on_error(m, error)
        raise
    if applied and callbacks.after_migrate is not None:
        yield Fire(callbacks.after_migrate, (applied,))
    return applied


def run_up(
    m: MigratorBase,
    target: Optional[str],
    validate: bool,
    allow_out_of_order: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
    unrehearsed: bool,
) -> Core[List[str]]:
    """The run itself, without the callbacks up() wraps it in."""
    from sustained.exceptions import MigrationError

    if models is not None and target is not None:
        raise ValueError(
            "up() cannot take both models and a target: the generated "
            "migration always runs last, so a target would leave it out."
        )
    require_registered = models is None

    def locked() -> Core[List[str]]:
        if models is not None:
            yield from bookkeeping.ensure_tracking_table(m)

        migrations = m._versioned()
        if target is not None:
            ids = [x.id for x in migrations]
            if target not in ids:
                if any(x.id == target for x in m._repeatables()):
                    raise ValueError(
                        f"Migration target {target!r} is repeatable; a "
                        "target must name a versioned migration."
                    )
                raise ValueError(f"Unknown migration target: {target!r}.")
            migrations = migrations[: ids.index(target) + 1]

        records = yield from bookkeeping.applied_records(m)
        if validate:
            problems = _validation_problems(
                m._migrations,
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
        versioned_now = [x for x in migrations if x.id not in already_applied]
        repeatables_now = [
            x
            for x in (m._repeatables() if target is None else [])
            if not _is_current(records_by_id.get(x.id), x, True)
        ]
        # The registered set is checked before anything runs. The
        # order matches pending(), so a rehearsal of the same set
        # produces the same key.
        registered_run = versioned_now + repeatables_now
        warned: Set["Verdict"] = set()
        check_guards(m._guards, registered_run, m._dialect, warned)
        yield from bookkeeping.require_rehearsal_row(
            m, records, registered_run, unrehearsed, target
        )
        final_run = list(registered_run)
        # A migration applied before a failure stays applied and
        # committed, so the error lists it for the caller.
        try:
            for migration in versioned_now:
                yield from apply(m, migration, next_seq, update=False)
                next_seq += 1
                applied_now.append(migration.id)
            if models is not None:
                generated = yield from plan(
                    m,
                    models,
                    allow_drops=allow_drops,
                    ignore_changed_columns=ignore_changed_columns,
                    migration_id=migration_id,
                    renames=renames,
                    table_renames=table_renames,
                    type_casts=type_casts,
                )
                if generated is not None:
                    # The generated statements are known only now, after
                    # the registered migrations left the schema they diff
                    # against, so both gates run a second time before the
                    # one migration they could not see. The registered
                    # migrations are already applied and committed by
                    # then, so a block here reports what it stopped after.
                    final_run = registered_run + [generated]
                    check_guards(m._guards, final_run, m._dialect, warned)
                    yield from bookkeeping.require_rehearsal_row(
                        m, records, final_run, unrehearsed, target
                    )
                    # The migration joins the registered list only after
                    # it applied. A failed one left there would run again
                    # on the next up() of a long-lived migrator, and would
                    # run alongside a fresh diff of the same models.
                    yield from apply(
                        m, generated, next_seq, update=False, generated=True
                    )
                    m._migrations.append(generated)
                    next_seq += 1
                    applied_now.append(generated.id)
            for migration in repeatables_now:
                record = records_by_id.get(migration.id)
                yield from apply(m, migration, next_seq, update=record is not None)
                if record is None:
                    next_seq += 1
                applied_now.append(migration.id)
            if unrehearsed and _destructive_in(final_run, m._compiler):
                # The proof was waived, so the row says so. It never
                # unlocks a later run: only 'passed' does that.
                yield from bookkeeping.record_rehearsal(
                    m, rehearsal_key(records, final_run), REHEARSAL_OVERRIDE
                )
            return applied_now
        except Exception as error:
            _tag_applied(error, applied_now)
            raise

    return (yield from bookkeeping.lock_scope(m, locked()))


def apply(
    m: MigratorBase,
    migration: Migration,
    seq: int,
    update: bool,
    generated: bool = False,
) -> Core[None]:
    """
    Runs one migration's up step and records it: an INSERT for a
    first run, an UPDATE in place when a repeatable re-runs, keeping
    its original seq. `generated` marks a migration the diff against
    the models produced, whose id nothing on disk carries. Its
    statements go on the tracking row, so down() can take it back.
    """
    try:
        yield from migration_scope(
            m,
            _apply_body(m, migration, seq, update, generated),
            migration.transactional,
        )
    except Exception as error:
        yield from bookkeeping.record_failure(
            m, migration, seq, update=update, generated=generated
        )
        _tag_migration(error, migration.id)
        raise


def _apply_body(
    m: MigratorBase, migration: Migration, seq: int, update: bool, generated: bool
) -> Core[None]:
    """The step and its tracking row, inside the migration's scope."""
    started = time.perf_counter()
    yield RunStep(migration.up)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    timestamp = datetime.now(timezone.utc).isoformat()
    checksum = migration_checksum(migration)
    steps = _stored_steps(migration, generated, m._compiler)
    # The row is written on the transaction's cursor where a block is
    # open, so a failed migration takes its row back with it on the
    # engines that roll DDL back, and on DuckDB, where every fresh cursor
    # is a session of its own.
    if update:
        yield Execute(
            m._update_sql(),
            (checksum, timestamp, elapsed_ms, True, generated, steps, migration.id),
            pinned=True,
        )
    else:
        yield Execute(
            m._insert_sql(),
            (
                migration.id,
                seq,
                checksum,
                timestamp,
                elapsed_ms,
                True,
                generated,
                steps,
            ),
            pinned=True,
        )


def revert_body(
    m: MigratorBase, step: MigrationStep, migration_id: str, pinned: bool
) -> Core[None]:
    """
    One migration's down step and the removal of its tracking row, inside
    the migration's scope. `pinned` is Execute's flag for the removal.
    """
    yield RunStep(step)
    yield Execute(
        f"DELETE FROM {m._table_sql()} WHERE "
        f"{m._compiler.quote_identifier('id')} = {m._compiler.placeholder()}",
        (migration_id,),
        pinned=pinned,
    )


def applied_versioned(
    m: MigratorBase, ids: Optional[List[str]] = None
) -> Core[List[str]]:
    """
    Applied ids with the repeatables left out; down() skips them. The
    ids are read from the tracking table unless the caller passes a
    list it has already read.
    """
    repeatable_ids = {x.id for x in m._repeatables()}
    source = (yield from bookkeeping.applied(m)) if ids is None else ids
    return [i for i in source if i not in repeatable_ids]


def down_to(m: MigratorBase, target: str, allow_changed: bool) -> Core[List[str]]:
    yield RefuseOpenTransaction("down_to")
    applied = yield from applied_versioned(m)
    if target not in applied:
        raise ValueError(f"Migration '{target}' is not applied.")
    steps = len(applied) - applied.index(target) - 1
    if not steps:
        return []
    return (yield from down(m, steps, allow_changed))


def down(m: MigratorBase, steps: int, allow_changed: bool) -> Core[List[str]]:
    _checked_steps(steps)
    yield RefuseOpenTransaction("down")
    try:
        return (yield from run_down(m, steps, allow_changed))
    except Exception as error:
        yield from fire_on_error(m, error)
        raise


def run_down(m: MigratorBase, steps: int, allow_changed: bool) -> Core[List[str]]:
    """The run itself, without the callback down() wraps it in."""
    from sustained.exceptions import MigrationError

    def locked() -> Core[List[str]]:
        yield from bookkeeping.ensure_tracking_table(m)
        records = yield from bookkeeping.read_records(m)
        failed = [_failed_attempt_problem(r.id) for r in records if not r.success]
        if failed:
            raise MigrationError(failed)
        by_record = {r.id: r for r in records}
        applied = yield from applied_versioned(m, [r.id for r in records if r.success])
        by_id = {x.id: x for x in m._migrations}
        reverted: List[str] = []
        # Every migration in the window is read and checked before the
        # first one is reverted. A refusal in the middle of the loop
        # would leave the newer migrations reverted and committed for a
        # condition that was knowable before any of them ran.
        window: List[Tuple[str, Migration, MigrationStep]] = []
        for migration_id in reversed(applied[-steps:] if steps else []):
            migration = by_id.get(migration_id)
            if migration is not None and not allow_changed:
                if _changed_since_applied(migration, by_record.get(migration_id)):
                    raise MigrationError([_changed_down_message(migration_id)])
            if migration is None:
                migration = yield from bookkeeping.generated_migration(m, migration_id)
            if migration is None:
                raise ValueError(
                    f"Applied migration '{migration_id}' is not registered "
                    "with this migrator; cannot revert."
                )
            if migration.down is None:
                raise ValueError(f"Migration '{migration_id}' has no down step.")
            window.append((migration_id, migration, migration.down))
        for migration_id, migration, down_step in window:
            try:
                yield from migration_scope(
                    m,
                    revert_body(m, down_step, migration_id, pinned=True),
                    migration.transactional,
                )
            except Exception as error:
                yield from bookkeeping.record_down_failure(m, migration)
                _tag_migration(error, migration_id)
                raise
            reverted.append(migration_id)
        return reverted

    return (yield from bookkeeping.lock_scope(m, locked()))


def plan(
    m: MigratorBase,
    models: List[Type["Model"]],
    allow_drops: bool = False,
    ignore_changed_columns: bool = False,
    migration_id: Optional[str] = None,
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    type_casts: Optional[Dict[str, str]] = None,
    ignore_undeclared: bool = True,
    snapshot: Optional["Snapshot"] = None,
) -> Core[Optional[Migration]]:
    """
    The migration a diff of the models produces. The async driver's
    source is a replay of a schema read, which writes nothing and cannot
    ask whether a table holds a row, so a table it cannot read counts as
    one that holds rows there. The snapshot read for the replay is not
    passed on: the diff reads the replay itself.
    """
    from sustained.autogenerate import declared_schemas

    source: Tuple[Connection, Optional["Snapshot"]] = yield DiffSource(
        declared_schemas(models)
    )
    return plan_migration(
        source[0],
        models,
        m._dialect,
        m._own_tables(),
        allow_drops=allow_drops,
        ignore_changed_columns=ignore_changed_columns,
        migration_id=migration_id,
        renames=renames,
        table_renames=table_renames,
        type_casts=type_casts,
        ignore_undeclared=ignore_undeclared,
        snapshot=snapshot,
    )


def drift(
    m: MigratorBase,
    models: List[Type["Model"]],
    renames: Optional[Dict[str, str]] = None,
    table_renames: Optional[Dict[str, str]] = None,
    ignore_changed_columns: bool = False,
) -> Core[List[str]]:
    """
    What the models still ask for. The async driver hands over the
    snapshot it read along with the replay, so the diff reads no schema
    of its own; the blocking driver hands over none, and the diff reads
    the connection.
    """
    from sustained.autogenerate import declared_schemas

    source: Tuple[Connection, Optional["Snapshot"]] = yield DiffSource(
        declared_schemas(models)
    )
    connection, snapshot = source
    return drift_lines(
        connection,
        models,
        m._dialect,
        m._own_tables(),
        renames=renames,
        table_renames=table_renames,
        ignore_changed_columns=ignore_changed_columns,
        snapshot=snapshot,
    )
