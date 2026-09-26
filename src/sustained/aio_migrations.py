"""
Async migration runner.

AsyncMigrator mirrors Migrator on an AsyncAdapter: same Migration objects,
same tracking table, same ordering rules. String and list steps execute
through the adapter; callable steps receive the adapter and are awaited
when they return a coroutine. Each migration runs inside an
async_transaction() block.

The whole Migrator surface is here, model diffing included. script(),
plan(), drift(), up(models=[...]) and rehearse(models=[...]) return what
the synchronous ones return. The schema read runs through the adapter,
and the diffing code reads the recording of that read.

Both migrators run the same code: the runs live in
sustained.migrations.core as generators that yield what they need done,
and this module answers those requests on the adapter.
"""

from __future__ import annotations

import inspect
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from sustained.aio import (
    AsyncAdapter,
    async_transaction,
    in_async_transaction,
    pinned_async_transaction,
)
from sustained.dialects import Dialects
from sustained.migrations import (
    REHEARSAL_PASSED,
    AppliedRecord,
    Callbacks,
    Migration,
    MigrationStep,
    Rehearsal,
    SchemaRead,
    _render_elements,
    _step_elements,
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
from sustained.types import RowValue, SqlValue

if TYPE_CHECKING:
    from sustained.guards import Guard
    from sustained.introspect import Snapshot
    from sustained.model import Model
    from sustained.schema import TableOptions


class AsyncMigrator(MigratorBase):
    """Applies and reverts an ordered list of migrations on an adapter."""

    def __init__(
        self,
        adapter: AsyncAdapter,
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
        self._adapter = adapter

    @property
    def adapter(self) -> AsyncAdapter:
        """The adapter this migrator runs on."""
        return self._adapter

    async def _drive(self, core: Core[T]) -> T:
        """
        Runs a piece of the core to its end on the adapter. Each request
        it yields is awaited here; a request that raises has its error
        thrown back in at the yield, so the core handles it where the
        statement ran, and an error the core does not handle leaves here
        unchanged.
        """
        try:
            request = next(core)
            while True:
                try:
                    result = await self._perform(request)
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

    async def _perform(self, request: Request) -> Any:
        """Answers one request on the adapter."""
        adapter = self._adapter
        if isinstance(request, Execute):
            # The adapter keeps a block's statements on one session, so a
            # pinned statement needs nothing more.
            if request.params is None:
                await adapter.execute(request.sql, ())
            else:
                await self._execute(request.sql, request.params)
            return None
        if isinstance(request, Fetch):
            if request.params is None:
                _, rows = await adapter.fetch(request.sql, ())
            else:
                _, rows = await self._fetch(request.sql, request.params)
            return rows
        if isinstance(request, ExecuteMany):
            for sql, batch in request.batches:
                await adapter.executemany(sql, batch)
            return None
        if isinstance(request, TakeLock):
            _, rows = await adapter.fetch(request.statement, ())
            return rows[0] if rows else None
        if isinstance(request, RunStep):
            await self._run_step(request.step)
            return None
        if isinstance(request, Commit):
            await adapter.commit()
            return None
        if isinstance(request, Rollback):
            await adapter.rollback()
            return None
        if isinstance(request, Fire):
            result = request.hook(adapter, *request.args)
            if inspect.isawaitable(result):
                await result
            return None
        if isinstance(request, ReadSchema):
            schema, _ = await self._read_schema()
            return schema
        if isinstance(request, DiffSource):
            snapshot, read = await self._read_schema(request.schemas)
            return read.connection(), snapshot
        if isinstance(request, RefuseOpenTransaction):
            self._refuse_open_transaction(request.verb)
            return None
        if isinstance(request, RefuseRehearsal):
            self._refuse_rehearsal()
            return None
        if isinstance(request, Transaction):
            async with async_transaction(adapter, self._dialect):
                return await self._drive(request.body)
        if isinstance(request, Autocommit):
            async with adapter.autocommit_scope():
                return await self._drive(request.body)
        if isinstance(request, Session):
            async with adapter.session():
                return await self._drive(request.body)
        if isinstance(request, PinnedTransaction):
            async with pinned_async_transaction(adapter):
                return await self._drive(request.body)
        if isinstance(request, BeginPinned):
            begin = self._compiler.begin_transaction_sql()
            if begin is not None:
                await adapter.execute(begin, ())
            return None
        raise TypeError(f"Unknown migrator request: {request!r}")

    async def _run_step(self, step: MigrationStep) -> None:
        elements = _step_elements(step)
        if elements is None:
            assert callable(step)
            result = step(self._adapter)
            if inspect.isawaitable(result):
                await result
            return
        for statement in _render_elements(elements, self._compiler):
            await self._adapter.execute(statement, ())

    async def _execute(self, sql: str, params: Tuple[SqlValue, ...]) -> None:
        """Runs one parameterized statement, adapted for the dialect."""
        await self._adapter.execute(*self._compiler.prepare_execution(sql, params))

    async def _fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        """Runs one parameterized query, adapted for the dialect."""
        return await self._adapter.fetch(*self._compiler.prepare_execution(sql, params))

    def _refuse_open_transaction(self, verb: str) -> None:
        """
        Raises when an async_transaction() block is open on the adapter.
        The run commits its own work as it goes, and that commit would
        take the caller's uncommitted work with it.
        """
        if in_async_transaction(self._adapter):
            raise ValueError(
                f"{verb} cannot run inside an open async_transaction() "
                "block: it commits as it goes, and the commit would take "
                "the caller's work with it."
            )

    def _refuse_rehearsal(self) -> None:
        """Raises when a rehearsal's rollback could not take its work back."""
        connection = getattr(self._adapter, "_connection", None)
        if getattr(connection, "autocommit", False) is True:
            raise ValueError(
                "rehearse cannot run on a connection in autocommit mode: "
                "nothing would roll back. Open the connection without "
                "autocommit, or point rehearse at a scratch database."
            )
        if in_async_transaction(self._adapter):
            raise ValueError(
                "rehearse cannot run inside an open async_transaction() "
                "block: its rollback would take the caller's work back too."
            )

    async def _read_schema(
        self, schemas: Sequence[str] = ()
    ) -> Tuple["Snapshot", SchemaRead]:
        """
        Reads the live schema through the adapter and records the read.

        async_introspect_schema() reads the same way. This one keeps every
        statement and the rows it returned, so plan() can hand the
        recording to autogenerate(), which reads a schema through a
        blocking connection.

        `schemas` covers the schemas the models name on top of the one
        the connection is on, and must be what the replaying code reads
        with. autogenerate() reads with declared_schemas(models), so a
        caller that replays a recording into it reads the same schemas
        here. A recording made with a different scope holds different
        statements, and the replay refuses it.

        The read runs through async_introspect_schema(), so it takes the
        same per-statement savepoints a guarded read takes: on Postgres
        one failed catalog probe would otherwise stop every statement
        after it in the same transaction.
        """
        from sustained.introspect import async_introspect_schema

        read = SchemaRead()
        snapshot = await async_introspect_schema(
            self._adapter, self._dialect, tuple(schemas), recorder=read
        )
        return snapshot, read

    async def record_rehearsal(self, key: str, outcome: str = REHEARSAL_PASSED) -> None:
        """
        Writes the row for one rehearsal key, replacing any earlier row
        for the same key. Mirrors Migrator.record_rehearsal().
        """
        await self._drive(bookkeeping.record_rehearsal(self, key, outcome))

    async def record_scratch_rehearsal(self, results: Rehearsal) -> Optional[str]:
        """
        Writes the rows a passing scratch rehearsal proves on this
        database and returns the full run's key, or None when nothing was
        written. Mirrors Migrator.record_scratch_rehearsal().
        """
        return await self._drive(bookkeeping.record_scratch_rehearsal(self, results))

    async def rehearsal_outcome(self, key: str) -> Optional[str]:
        """
        The outcome recorded for this key: 'passed' or 'failed' from a
        rehearsal, 'override' from a run with unrehearsed=True, or None when
        no row covers it.
        """
        return await self._drive(bookkeeping.rehearsal_outcome(self, key))

    async def rehearsed(self, key: str) -> bool:
        """True when a passing rehearsal covers this key."""
        return await self._drive(bookkeeping.rehearsed(self, key))

    async def run_outcome(
        self, applied: Sequence[AppliedRecord], run: Sequence[Migration]
    ) -> Optional[str]:
        """
        The outcome recorded for a run of these migrations from this
        applied history, as up() reads it. Mirrors Migrator.run_outcome().
        """
        return await self._drive(bookkeeping.run_outcome(self, applied, run))

    async def applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row in application order, creating
        the tracking table when it is missing.
        """
        return await self._drive(bookkeeping.applied_records(self))

    async def read_applied_records(self) -> List[AppliedRecord]:
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
        return await self._drive(bookkeeping.read_applied_records(self))

    async def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return await self._drive(bookkeeping.applied(self))

    async def read_applied(self) -> List[str]:
        """The applied migration ids, without creating the table."""
        return await self._drive(bookkeeping.read_applied(self))

    async def script(self, direction: str = "up") -> str:
        """
        Renders the SQL a run would execute, without executing anything,
        for review or DBA handoff. 'up' renders every pending migration;
        'down' renders the applied migrations newest-first. Tracking table
        bookkeeping statements are included.

        Nothing is written, not even the tracking table: a database
        without one reads as a database with no migrations applied.
        Migrator.script() renders the same text.
        """
        return await self._drive(bookkeeping.script(self, direction))

    async def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        return await self._drive(bookkeeping.pending(self))

    async def status(self) -> List[Tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        return await self._drive(bookkeeping.status(self))

    async def statuses(self) -> List[Tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        return await self._drive(bookkeeping.statuses(self))

    async def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        return await self._drive(bookkeeping.validate(self, raise_on_problems))

    async def repair(self) -> List[str]:
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
        return await self._drive(bookkeeping.repair(self))

    async def baseline(self, target: str) -> List[str]:
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
        return await self._drive(bookkeeping.baseline(self, target))

    async def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
        *,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[Dict[str, str]] = None,
        table_renames: Optional[Dict[str, str]] = None,
        type_casts: Optional[Dict[str, str]] = None,
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
        without the proof.

        The migrator's guards read the statements before they run. A
        blocking verdict raises GuardBlocked and nothing is applied; a
        warning prints on stderr. Both gates read the registered
        migrations before anything runs, and read them again together
        with the generated migration, whose statements exist only once
        the registered ones have applied. A block or a missing row at
        that second reading leaves the registered migrations applied.
        Any error raised after a migration applied lists the ids that
        applied on the exception's `applied` attribute. The
        migrator's callbacks fire around the run, and each is awaited
        when it returns an awaitable.
        """
        return await self._drive(
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

    async def rehearse(
        self,
        scratch: bool = False,
        models: Optional[List[Type["Model"]]] = None,
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[Dict[str, str]] = None,
        table_renames: Optional[Dict[str, str]] = None,
        type_casts: Optional[Dict[str, str]] = None,
    ) -> Rehearsal:
        """
        Runs every pending migration up, then back down, inside one
        transaction, and rolls that transaction back. Returns one result
        per migration that ran; an empty list means nothing was pending.
        Mirrors Migrator.rehearse(), including the dialect check and the
        scratch=True waiver for a database that can be thrown away.

        With models, the run rehearses what up(models=[...]) would apply:
        the generated migration joins the pending list for this run only,
        and its result reports whether the schema then matched the models.
        The remaining arguments are the diff options up() takes, and they
        should match the ones the real run will use.

        The schema is read before the run and again after the down sweep,
        and a difference between the two means a down step ran without
        taking its change back. The comparison is only made when every
        step in the run reversed. Tables and columns are compared;
        indexes, constraints, and defaults are not.

        A passing run leaves a rehearsal row behind, which up() reads before it
        applies anything that removes data. A scratch rehearsal records
        nothing; the key comes back on the result for the caller to record
        on the database the next run will read.

        A migration with transactional=False is left out of the run: its
        statements refuse or ignore a transaction block, and the rehearsal
        runs inside one. Its result reports up_ok as None with the reason,
        the run can still pass, and the row a passing run records covers
        it without proof.
        """
        return await self._drive(
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

    async def plan(
        self,
        models: List[Type["Model"]],
        allow_drops: bool = False,
        ignore_changed_columns: bool = False,
        migration_id: Optional[str] = None,
        renames: Optional[Dict[str, str]] = None,
        table_renames: Optional[Dict[str, str]] = None,
        type_casts: Optional[Dict[str, str]] = None,
        ignore_undeclared: bool = True,
    ) -> Optional[Migration]:
        """
        Diffs the database against the models and returns the migration
        up(models=[...]) would generate, without registering or applying
        it. Returns None when the schema is already up to date. The
        tracking table is excluded from the diff. Mirrors
        Migrator.plan(), and writes nothing: the schema read is the only
        statement it runs. Because of that it cannot ask whether a table
        holds a row, and a table it cannot read counts as one that holds
        rows. A new NOT NULL column with no default and no backfill is
        refused here even on an empty table, where Migrator.plan() adds
        it.

        Objects the models do not declare are left alone, since a
        database may hold tables that hand-written migrations created.
        Pass allow_drops=True to generate the drops instead, or
        ignore_undeclared=False to refuse to generate while they exist.
        """
        return await self._drive(
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
            )
        )

    async def drift(
        self,
        models: List[Type["Model"]],
        renames: Optional[Dict[str, str]] = None,
        table_renames: Optional[Dict[str, str]] = None,
        ignore_changed_columns: bool = False,
    ) -> List[str]:
        """
        What the models still ask for, one readable line each, empty when
        the database holds everything they declare. Mirrors
        Migrator.drift(), including the lines it returns.

        Objects the database holds and the models do not are left out. A
        generated migration leaves those alone unless drops are allowed,
        so a schema built partly by hand does not read as drift here. Use
        plan() for the full comparison, drops included.

        Pass ignore_changed_columns=True to leave type and nullability
        changes out, matching a run that generates its migration the same
        way.
        """
        return await self._drive(
            runs.drift(
                self,
                models,
                renames=renames,
                table_renames=table_renames,
                ignore_changed_columns=ignore_changed_columns,
            )
        )

    async def down(self, steps: int = 1, allow_changed: bool = False) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step. Repeatables are never
        reverted. Returns the ids that were reverted.

        A migration generated from the models is reverted from its own
        tracking row, which holds the statements it ran. Every other
        migration must be registered with this migrator.

        `steps` counts migrations and must be 0 or more. A count of 0
        reverts nothing and returns an empty list.

        A migration whose statements changed since it was applied raises
        MigrationError, because its down step describes the new contents
        and the database holds the old ones. Pass allow_changed=True to
        revert it with the down step as it stands now.

        Every migration in the window is read and checked first, so a
        refusal reverts nothing. A failed attempt on record refuses the
        run, and a down step that fails where nothing rolls it back marks
        the migration's row failed, as Migrator.down() does. The
        migrator's on_error callback fires for a failed run.
        """
        return await self._drive(runs.down(self, steps, allow_changed))

    async def down_to(self, target: str, allow_changed: bool = False) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted. `allow_changed` is passed to
        down().
        """
        return await self._drive(runs.down_to(self, target, allow_changed))
