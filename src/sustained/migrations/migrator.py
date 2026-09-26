"""
The Migrator: applies and reverts an ordered list of migrations on one
blocking connection, and keeps the tracking and rehearsal tables.

The runs themselves live in sustained.migrations.core, written once for
Migrator and AsyncMigrator. This module answers their requests on a
blocking connection.
"""

from __future__ import annotations

import warnings
from contextlib import closing
from typing import (
    TYPE_CHECKING,
    Any,
    ContextManager,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from sustained.dialects import Dialects
from sustained.execution import (
    cursor_scope,
    enter_autocommit,
    in_transaction,
    pinned_transaction,
    transaction,
)
from sustained.migrations.core import bookkeeping, rehearsing, runs
from sustained.migrations.core.base import MigratorBase
from sustained.migrations.core.requests import (
    Autocommit,
    BeginPinned,
    Commit,
    Core,
    DiffSource,
    Execute,
    ExecuteMany,
    Fetch,
    Fire,
    PinnedTransaction,
    ReadSchema,
    RefuseOpenTransaction,
    RefuseRehearsal,
    Request,
    Rollback,
    RunStep,
    Session,
    T,
    TakeLock,
    Transaction,
)
from sustained.migrations.migration import (
    AppliedRecord,
    Callbacks,
    Migration,
    _run_step,
)
from sustained.migrations.rehearsal import REHEARSAL_PASSED, Rehearsal
from sustained.migrations.tracking import _lock_row
from sustained.types import Connection, Cursor, SqlValue

if TYPE_CHECKING:
    from sustained.guards import Guard
    from sustained.introspect import Snapshot
    from sustained.model import Model
    from sustained.schema import TableOptions


class Migrator(MigratorBase):
    """
    Applies and reverts an ordered list of migrations on one connection.

    Applied migration ids live in a tracking table, created on first use.
    Each migration runs inside a transaction, so a failing step leaves the
    schema at the previous migration. A migration built with
    transactional=False is the exception: it runs bare, and a failing step
    there leaves the statements before it applied. Engines that do not
    support transactional DDL may still leave partial changes from a
    multi-step migration. Engines without transactions at all, such as Athena, run
    each step bare; a failing migration there can leave partial changes
    that need manual cleanup.

    `guards` are rules that read the statements a run would apply; see
    sustained.guards. A blocking verdict stops up() before any statement
    runs. `callbacks` are the functions to call around a run.
    """

    def __init__(
        self,
        connection: Connection,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
        tracking_table_options: Optional["TableOptions"] = None,
        rehearsal_table: str = "sustained_rehearsals",
        guards: Optional[Sequence["Guard"]] = None,
        callbacks: Optional[Callbacks] = None,
    ) -> None:
        super().__init__(
            migrations,
            table,
            dialect,
            tracking_table_options,
            rehearsal_table,
            guards,
            callbacks,
        )
        self._connection = connection

    @property
    def connection(self) -> Connection:
        """The connection this migrator runs on."""
        return self._connection

    def _drive(self, core: Core[T]) -> T:
        """
        Runs a piece of the core to its end on the connection. Each request
        it yields is answered here; a request that raises has its error
        thrown back in at the yield, so the core handles it where the
        statement ran, and an error the core does not handle leaves here
        unchanged.
        """
        try:
            request = next(core)
            while True:
                try:
                    result = self._perform(request)
                except BaseException as error:
                    failure = error
                else:
                    request = core.send(result)
                    continue
                # The throw sits outside the except block, so an error the
                # core raises later is not chained to one it already
                # handled.
                try:
                    request = core.throw(failure)
                finally:
                    del failure
        except StopIteration as stop:
            value: T = stop.value
            return value

    def _perform(self, request: Request) -> Any:
        """Answers one request on the connection."""
        connection = self._connection
        if isinstance(request, Execute):
            if request.params is not None:
                if request.pinned:
                    self._write_tracking_row(request.sql, request.params)
                else:
                    self._run_sql(request.sql, request.params)
                return None
            scope: ContextManager[Cursor] = (
                cursor_scope(connection)
                if request.pinned
                else closing(connection.cursor())
            )
            with scope as cursor:
                cursor.execute(request.sql)
            return None
        if isinstance(request, Fetch):
            with closing(connection.cursor()) as cursor:
                if request.params is None:
                    cursor.execute(request.sql)
                else:
                    self._execute(cursor, request.sql, request.params)
                return list(cursor.fetchall())
        if isinstance(request, ExecuteMany):
            with closing(connection.cursor()) as cursor:
                for sql, rows in request.batches:
                    cursor.executemany(sql, rows)
            return None
        if isinstance(request, TakeLock):
            with closing(connection.cursor()) as cursor:
                cursor.execute(request.statement)
                return _lock_row(cursor)
        if isinstance(request, RunStep):
            _run_step(connection, request.step, self._compiler)
            return None
        if isinstance(request, Commit):
            if hasattr(connection, "commit"):
                connection.commit()
            return None
        if isinstance(request, Rollback):
            if hasattr(connection, "rollback"):
                connection.rollback()
            return None
        if isinstance(request, Fire):
            request.hook(connection, *request.args)
            return None
        if isinstance(request, ReadSchema):
            from sustained.autogenerate import introspect_schema

            return introspect_schema(connection, self._dialect)
        if isinstance(request, DiffSource):
            # The diff reads the connection itself, and can ask whether a
            # table holds rows.
            return connection, None
        if isinstance(request, RefuseOpenTransaction):
            self._refuse_open_transaction(request.verb)
            return None
        if isinstance(request, RefuseRehearsal):
            self._refuse_rehearsal()
            return None
        if isinstance(request, Transaction):
            with transaction(connection, self._dialect):
                return self._drive(request.body)
        if isinstance(request, Autocommit):
            # The driver's own transaction control is off for the block
            # and back on at the end. See enter_autocommit().
            restore = enter_autocommit(connection)
            try:
                return self._drive(request.body)
            finally:
                restore()
        if isinstance(request, Session):
            return self._drive(request.body)
        if isinstance(request, PinnedTransaction):
            with pinned_transaction(connection, self._dialect):
                return self._drive(request.body)
        if isinstance(request, BeginPinned):
            # pinned_transaction() sent the BEGIN on the way in.
            return None
        raise TypeError(f"Unknown migrator request: {request!r}")

    def _execute(
        self, cursor: "Cursor", sql: str, params: Tuple[SqlValue, ...]
    ) -> None:
        """Runs one parameterized statement, adapted for the dialect."""
        cursor.execute(*self._compiler.prepare_execution(sql, params))

    def _run_sql(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One statement on a cursor of its own, given back when it finishes.
        A cursor left open holds its result set, and pyodbc and the MySQL
        drivers refuse the next statement on the connection once enough of
        those pile up.
        """
        with closing(self._connection.cursor()) as cursor:
            self._execute(cursor, sql, params)

    def _write_tracking_row(self, sql: str, params: Tuple[SqlValue, ...] = ()) -> None:
        """
        One tracking table write inside the migration's own transaction.
        It runs on the transaction's cursor where a block is open, so a
        failed migration takes its row back with it on the engines that
        roll DDL back, and on DuckDB, where every fresh cursor is a session
        of its own.
        """
        with cursor_scope(self._connection) as cursor:
            self._execute(cursor, sql, params)

    def _refuse_open_transaction(self, verb: str) -> None:
        """
        Raises when a transaction() block is open on the connection. The
        run commits its own work as it goes, and that commit would take
        the caller's uncommitted work with it.
        """
        if in_transaction(self._connection):
            raise ValueError(
                f"{verb} cannot run inside an open transaction() block: "
                "it commits as it goes, and the commit would take the "
                "caller's work with it."
            )

    def _refuse_rehearsal(self) -> None:
        """Raises when a rehearsal's rollback could not take its work back."""
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

    def record_rehearsal(self, key: str, outcome: str = REHEARSAL_PASSED) -> None:
        """
        Writes the row for one rehearsal key, replacing any earlier row
        for the same key. The outcome is 'passed', 'failed', or 'override'
        for statements applied with unrehearsed=True.

        A rehearsal on a scratch database records nothing on its own: the
        row belongs on the database the next run will read. Pass its
        result to record_scratch_rehearsal() on a migrator bound to that
        database.
        """
        self._drive(bookkeeping.record_rehearsal(self, key, outcome))

    def record_scratch_rehearsal(self, results: Rehearsal) -> Optional[str]:
        """
        Writes the rows a passing rehearsal on a scratch database proves,
        on the database this migrator is bound to, and returns the key of
        the full run. Returns None and writes nothing when the rehearsal
        failed, nothing is pending here, or the scratch run did not run
        every migration pending here.

        The scratch database starts from its own schema, so the keys are
        computed against this database's applied history and pending set.
        A row also goes in for each shorter run a `target` would produce
        that removes data, the same rows a rehearsal on this database
        records, so a targeted up() finds its row too.
        """
        return self._drive(bookkeeping.record_scratch_rehearsal(self, results))

    def rehearsal_outcome(self, key: str) -> Optional[str]:
        """
        The outcome recorded for this key: 'passed' or 'failed' from a
        rehearsal, 'override' from a run with unrehearsed=True, or None when
        no row covers it.
        """
        return self._drive(bookkeeping.rehearsal_outcome(self, key))

    def rehearsed(self, key: str) -> bool:
        """True when a passing rehearsal covers this key."""
        return self._drive(bookkeeping.rehearsed(self, key))

    def run_outcome(
        self, applied: Sequence[AppliedRecord], run: Sequence[Migration]
    ) -> Optional[str]:
        """
        The outcome recorded for a run of these migrations from this
        applied history, as up() reads it. It looks up rehearsal_key()
        first. When no row has that key, it looks up the key a release
        before 2.25.0 wrote for the same run, so a rehearsal recorded
        before an upgrade still covers the run after it.
        """
        return self._drive(bookkeeping.run_outcome(self, applied, run))

    def applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row in application order, creating
        the tracking table when it is missing.
        """
        return self._drive(bookkeeping.applied_records(self))

    def read_applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row without writing anything.

        An empty list means the run has no history to read: either the
        tracking table does not exist yet, or it has only the columns an
        earlier version wrote. Any other failed read, such as a closed
        connection or a refused SELECT, raises the driver's error, so a
        report never shows every migration pending on a database it could
        not read. The paths that only report on a run, such as script()
        and pending(), read the rows through this, since creating the
        table would change a database they say they leave alone.
        """
        return self._drive(bookkeeping.read_applied_records(self))

    def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return self._drive(bookkeeping.applied(self))

    def read_applied(self) -> List[str]:
        """The applied migration ids, without creating the table."""
        return self._drive(bookkeeping.read_applied(self))

    def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        return self._drive(bookkeeping.pending(self))

    def status(self) -> List[tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        return self._drive(bookkeeping.status(self))

    def statuses(self) -> List[tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        return self._drive(bookkeeping.statuses(self))

    def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        return self._drive(bookkeeping.validate(self, raise_on_problems))

    def repair(self) -> List[str]:
        """
        Brings the tracking table back in line with the registered
        migrations: deletes rows left by failed attempts and rewrites
        stored checksums that no longer match, including null checksums on
        rows written before checksums existed. Returns a description of
        every action taken. Schema changes a failed attempt left behind
        are not touched; clean those up first.

        A changed repeatable keeps its stored checksum. For it a changed
        checksum schedules a re-run, and rewriting the row here would
        cancel that run without the new contents ever reaching the
        database. A repeatable row that stores the checksum format of a
        release before 2.25.0 for unchanged statements is rewritten in the
        current format, like any other row.
        """
        return self._drive(bookkeeping.repair(self))

    def _record_failure(
        self,
        migration: Migration,
        seq: int,
        update: bool = False,
        generated: bool = False,
    ) -> None:
        """Writes a failed-attempt row; see bookkeeping.record_failure()."""
        self._drive(
            bookkeeping.record_failure(
                self, migration, seq, update=update, generated=generated
            )
        )

    def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
        *,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        type_casts: Optional[dict[str, str]] = None,
        unrehearsed: bool = False,
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

        Everything after allow_out_of_order is keyword-only, so a call
        written for an earlier release cannot bind a diff option to a
        positional argument it never meant.

        A run that would remove data stops unless a passing rehearsal
        covers exactly these statements against exactly this applied
        history. Rehearse first, or pass unrehearsed=True to apply them
        without the proof, which writes an 'override' row naming what
        was applied unproved. Runs that only add are never gated, and a
        callable step is invisible to the check, the same limit the
        destructive labels carry.

        The migrator's guards read the statements before they run. A
        blocking verdict raises GuardBlocked; a warning prints on stderr.
        Both gates read the registered migrations before anything runs,
        and read them again together with the generated migration, whose
        statements exist only once the registered ones have applied. A
        block or a missing row at that second reading leaves the
        registered migrations applied. Any error raised after a migration
        applied lists the ids that applied on the exception's `applied`
        attribute. The migrator's callbacks fire around the run.
        """
        return self._drive(
            runs.up(
                self,
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
        )

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
    ) -> Rehearsal:
        """
        Runs every pending migration up, then back down, inside one
        transaction, and rolls that transaction back. Returns one result
        per migration that ran; an empty result means nothing was pending.

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

        The up steps run in the order up() runs them: the versioned
        migrations, then the generated one, then the repeatables. The down
        steps then run newest-first, skipping the repeatables, which have
        none. The first migration without a down step stops the sweep,
        since everything older sits under changes that cannot be taken
        back. The first step that raises stops the rehearsal.

        The schema is read before the run and again after the down sweep.
        A difference between the two means a down step ran without taking
        its change back. The comparison is only made when every step in
        the run reversed: one migration without a down step leaves changes
        that no other step can be blamed for. Tables and columns are
        compared; indexes, constraints, and defaults are not, so a
        leftover index is not reported yet.

        A passing run leaves a rehearsal row behind, keyed to the applied
        history it started from and the statements it ran, which up() reads
        before it applies anything that removes data. A failing run records
        the failure under the same key. A scratch rehearsal records
        nothing, since the row belongs on the database the next run
        will read; the key comes back on the result for the caller to
        record there.

        Only dialects whose schema changes roll back may rehearse. Pass
        scratch=True when the connection points at a database that can be
        thrown away: the dialect check is then skipped, and the changes may
        survive the rollback.

        A migration with transactional=False is left out of the run: its
        statements refuse or ignore a transaction block, and the rehearsal
        runs inside one. Its result reports up_ok as None with the reason,
        the run can still pass, and the row a passing run records covers
        it without proof. What such a migration does can only be seen by
        running it.
        """
        return self._drive(
            rehearsing.rehearse(
                self,
                scratch=scratch,
                models=models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
            )
        )

    def drift(
        self,
        models: List[Type["Model"]],
        renames: Optional[dict[str, str]] = None,
        table_renames: Optional[dict[str, str]] = None,
        ignore_changed_columns: bool = False,
    ) -> List[str]:
        """
        What the models still ask for, one readable line each, empty when
        the database holds everything they declare.

        Objects the database holds and the models do not are left out. A
        generated migration leaves those alone unless drops are allowed,
        so a schema built partly by hand does not read as drift here. Use
        plan() for the full comparison, drops included.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a run that generates its migration the same
        way.
        """
        return self._drive(
            runs.drift(
                self,
                models,
                renames=renames,
                table_renames=table_renames,
                ignore_changed_columns=ignore_changed_columns,
            )
        )

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

        Raises MigrationError before it writes a row when a migration it
        would record has a failed attempt on record; run repair() first.
        A failure part way rolls back every row this call inserted.
        """
        return self._drive(bookkeeping.baseline(self, target))

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
        snapshot: Optional["Snapshot"] = None,
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

        Pass a snapshot from read_schema() to plan against it instead of
        reading the schema again. The snapshot is not changed, so one
        read can feed several plans.
        """
        return self._drive(
            runs.plan(
                self,
                models,
                allow_drops=allow_drops,
                ignore_changed_columns=ignore_changed_columns,
                migration_id=migration_id,
                renames=renames,
                table_renames=table_renames,
                type_casts=type_casts,
                ignore_undeclared=ignore_undeclared,
                snapshot=snapshot,
            )
        )

    def read_schema(self, models: List[Type["Model"]]) -> "Snapshot":
        """
        Reads the schema plan() diffs the models against: the
        connection's own schema plus every schema the models name.
        """
        from sustained.autogenerate import declared_schemas, introspect_schema

        return introspect_schema(
            self._connection, self._dialect, declared_schemas(models)
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

        Nothing is written, not even the tracking table: a database
        without one reads as a database with no migrations applied. A
        migration generated from the models renders its down step from
        the statements its tracking row stores, as down() reverts it.
        """
        return self._drive(bookkeeping.script(self, direction))

    def down_to(self, target: str, allow_changed: bool = False) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted. `allow_changed` is passed to
        down().
        """
        return self._drive(runs.down_to(self, target, allow_changed))

    def down(self, steps: int = 1, allow_changed: bool = False) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step. Repeatables are never
        reverted. Returns the ids that were reverted.

        A migration generated from the models is reverted from its own
        tracking row, which holds the statements it ran, so a process that
        never saw the diff can still take it back. Every other migration
        must be registered with this migrator.

        `steps` counts migrations and must be 0 or more. A count of 0
        reverts nothing and returns an empty list.

        A migration whose statements changed since it was applied raises
        MigrationError, because its down step describes the new contents
        and the database holds the old ones. Pass allow_changed=True to
        revert it with the down step as it stands now.

        Every migration in the window is read and checked first, so a
        refusal reverts nothing. A failed attempt on record refuses the
        run, because the window would skip that migration and revert the
        one before it.

        A down step that fails where nothing rolls it back, on an engine
        without transactional DDL or in a migration with
        transactional=False, marks the migration's row failed. Validation
        then blocks the next run until the revert is finished by hand and
        repair() removes the row. The migrator's on_error callback fires
        for a failed run.
        """
        return self._drive(runs.down(self, steps, allow_changed))
