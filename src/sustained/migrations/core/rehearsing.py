"""
A rehearsal: every pending migration up and back down inside one
transaction that rolls back, the schema read before and after, and the
row that records what the run proved.

Each function here is the body of a migrator method, or a helper of one,
written once for both migrators. What rehearse() promises is on its
docstring in Migrator; the notes here are about how.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Type, cast

from sustained.migrations.core import bookkeeping, runs
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    BeginPinned,
    Core,
    Execute,
    PinnedTransaction,
    ReadSchema,
    RefuseRehearsal,
    Session,
    rollback_quietly,
    run_in,
)
from sustained.migrations.migration import AppliedRecord, Migration, MigrationStep
from sustained.migrations.rehearsal import (
    REHEARSAL_FAILED,
    Rehearsal,
    RehearsalResult,
    _check_rehearsable,
    _down_sweep,
    _passed_rehearsal_keys,
    _rehearsal_results,
    _reversal_provable,
    _skipped_results,
    rehearsal_failed,
    rehearsal_key,
)
from sustained.migrations.tracking import _next_seq

if TYPE_CHECKING:
    from sustained.autogenerate import IntrospectedTable
    from sustained.introspect import Snapshot
    from sustained.model import Model

# What each migration's down step proved: down_ok and the error, if any.
Outcomes = Dict[str, Tuple[Optional[bool], Optional[str]]]


def rehearse(
    m: MigratorBase,
    scratch: bool,
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
) -> Core[Rehearsal]:
    if not scratch:
        _check_rehearsable(m._dialect)
    yield RefuseRehearsal()

    def locked() -> Core[Rehearsal]:
        yield from bookkeeping.validate(m)
        pending = yield from bookkeeping.pending(m)
        record_list = yield from bookkeeping.applied_records(m)
        if not pending and models is None:
            return Rehearsal([], rehearsal_key(record_list, []))
        if models is not None:
            yield from bookkeeping.ensure_tracking_table(m)
        before = yield from snapshot(m)
        # Close whatever transaction the reads above opened, so the
        # rehearsal's BEGIN starts a fresh one instead of warning.
        yield from rollback_quietly()
        # The rehearsal's statements share this transaction, so on an
        # engine that gives every cursor its own session they land in the
        # transaction the rollback takes back. The migrator is registered
        # as inside a transaction, so a callable step that runs a query
        # skips its commit and a nested transaction block takes a
        # savepoint.
        pinned: Tuple[List[RehearsalResult], Optional[Migration]] = yield from run_in(
            PinnedTransaction,
            _rehearse_pinned(
                m,
                pending,
                {r.id: r for r in record_list},
                _next_seq(record_list),
                before,
                models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
            ),
        )
        results, drift = pinned
        # What the row covers: the pending set, plus the generated
        # migration when the diff produced one.
        attempted = list(pending) + ([drift] if drift is not None else [])
        # The rehearsal row is written after the rollback, in its own
        # committed transaction, and still inside the lock: everything
        # the rehearsal itself wrote has just been taken back.
        key = rehearsal_key(record_list, attempted)
        passed = not any(rehearsal_failed(r) for r in results)
        recorded = False
        if not scratch:
            if passed:
                yield from bookkeeping.record_rehearsals(
                    m,
                    _passed_rehearsal_keys(
                        record_list, pending, key, drift is not None, m._compiler
                    ),
                )
            else:
                yield from bookkeeping.record_rehearsals(m, [key], REHEARSAL_FAILED)
            recorded = True
        return Rehearsal(results, key, recorded)

    # The lock sits outside the rehearsal transaction, so the rollback
    # runs before the lock is released. The state reads sit inside it,
    # so a concurrent migrator cannot apply between the read and the
    # rehearsal. The session keeps BEGIN, the rehearsed work and the
    # rollback on one database session: DbApiAsyncAdapter over DuckDB
    # would otherwise open a session per statement, and the work would
    # commit.
    return (yield from bookkeeping.lock_scope(m, run_in(Session, locked())))


def _rehearse_pinned(
    m: MigratorBase,
    pending: List[Migration],
    records: Dict[str, AppliedRecord],
    seq: int,
    before: Optional[Dict[str, "IntrospectedTable"]],
    models: Optional[List[Type["Model"]]],
    allow_drops: bool,
    ignore_changed_columns: bool,
    migration_id: Optional[str],
    renames: Optional[Dict[str, str]],
    table_renames: Optional[Dict[str, str]],
    type_casts: Optional[Dict[str, str]],
) -> Core[Tuple[List[RehearsalResult], Optional[Migration]]]:
    """
    The run inside the rehearsal transaction, which it takes back itself
    at the end whatever happened. Returns the results and the migration
    the diff against the models generated, if any.
    """
    m._rehearsing = True
    try:
        yield BeginPinned()
        ran: List[Migration] = []
        skipped: List[Migration] = []
        up_error: Optional[Tuple[str, str]] = None

        def apply_each(group: List[Migration]) -> Core[None]:
            nonlocal seq, up_error
            for migration in group:
                if not migration.transactional:
                    # The rehearsal runs inside one transaction,
                    # which this migration's statements refuse or
                    # ignore. It is reported as unproved rather
                    # than run and failed.
                    skipped.append(migration)
                    continue
                try:
                    yield from runs.apply(
                        m, migration, seq, update=migration.id in records
                    )
                except Exception as error:
                    up_error = (migration.id, str(error))
                    return
                seq += 1
                ran.append(migration)

        # The order matches up(): the versioned migrations, then
        # the generated one, then the repeatables, which may read
        # objects the generated migration creates.
        yield from apply_each([x for x in pending if not x.repeatable])
        landed: Dict[str, List[str]] = {}
        drift: Optional[Migration] = None
        if models is not None and up_error is None:
            # The diff is taken here, inside the rehearsal, so it
            # sees the schema the pending migrations just left. The
            # generated migration joins the run without being
            # registered: nothing outside the rehearsal should see a
            # migration the rollback is about to take back.
            drift = yield from runs.plan(
                m,
                models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
            )
            if drift is not None:
                if not drift.transactional:
                    # A generated SQLite rebuild says
                    # transactional=False, and its pragmas are
                    # ignored inside the rehearsal transaction.
                    skipped.append(drift)
                else:
                    try:
                        yield from runs.apply(
                            m, drift, seq, update=False, generated=True
                        )
                    except Exception as error:
                        up_error = (drift.id, str(error))
                    else:
                        seq += 1
                        ran.append(drift)
                        # The renames have already run, so the
                        # schema holds the new names. Passing the
                        # hints again would ask to rename objects
                        # that are gone.
                        landed[drift.id] = yield from runs.drift(
                            m,
                            models,
                            ignore_changed_columns=ignore_changed_columns,
                        )
        if up_error is None:
            yield from apply_each([x for x in pending if x.repeatable])
        outcomes = {} if up_error else (yield from rehearse_down(m, ran))
        reverted = None
        if before is not None and _reversal_provable(ran, outcomes):
            from sustained.autogenerate import diff_snapshots

            after = yield from snapshot(m)
            if after is not None:
                reverted = diff_snapshots(before, after)
        results = _rehearsal_results(
            ran, up_error, outcomes, landed, reverted
        ) + _skipped_results(skipped)
        return results, drift
    finally:
        m._rehearsing = False
        yield from roll_back_rehearsal(m)


def snapshot(m: MigratorBase) -> Core[Optional[Dict[str, "IntrospectedTable"]]]:
    """
    The live schema, without Sustained's own tables, or None when the
    database will not report it. A rehearsal compares two of these,
    and the tracking and rehearsal tables are created by the rehearsal
    itself, so leaving them in would report them as objects left
    behind.

    A read that raises leaves the rehearsal's other proofs standing
    and reports the comparison as not checked, which is what a
    scratch database on an engine Sustained cannot introspect gives.
    """
    try:
        schema: "Snapshot" = yield ReadSchema()
    except Exception:
        return None
    for name in m._own_tables():
        schema.pop(name.lower(), None)
    return dict(schema)


def roll_back_rehearsal(m: MigratorBase) -> Core[None]:
    """
    Takes back everything the rehearsal did. The statement runs first,
    on the rehearsal's own cursor, because a driver's own rollback()
    does nothing on connections that never opened a transaction of their
    own, and asyncpg runs in autocommit until a transaction is opened;
    the driver call follows to leave its bookkeeping straight.
    """
    statement = m._compiler.rollback_transaction_sql()
    if statement is not None:
        try:
            yield Execute(statement, pinned=True)
        except Exception:
            pass
    yield from rollback_quietly()


def rehearse_down(m: MigratorBase, ran: List[Migration]) -> Core[Outcomes]:
    """
    Runs the down steps of a rehearsal, newest-first, and reports what
    each one proved. A step that raises stops the sweep; the
    migrations under it report that they were not reached.
    """
    outcomes: Outcomes = {}
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
                yield from runs.migration_scope(
                    m,
                    runs.revert_body(
                        m,
                        cast(MigrationStep, migration.down),
                        migration.id,
                        pinned=False,
                    ),
                    migration.transactional,
                )
            except Exception as error:
                outcomes[migration.id] = (False, str(error))
                failed = migration.id
            else:
                outcomes[migration.id] = (True, None)
    return outcomes
