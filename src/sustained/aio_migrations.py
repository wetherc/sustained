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
"""

from __future__ import annotations

import inspect
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    cast,
)

from sustained.aio import AsyncAdapter, async_transaction
from sustained.dialects import Dialects
from sustained.migrations import (
    _UPGRADE_COLUMNS,
    REHEARSAL_FAILED,
    REHEARSAL_OVERRIDE,
    REHEARSAL_PASSED,
    AppliedRecord,
    CallbackResult,
    Callbacks,
    Migration,
    MigrationStep,
    Rehearsal,
    RehearsalResult,
    SchemaRead,
    _changed_down_message,
    _changed_since_applied,
    _check_rehearsable,
    _checked_steps,
    _destructive_in,
    _destructive_prefix_keys,
    _down_sweep,
    _is_current,
    _lock_message,
    _migration_state,
    _next_seq,
    _rehearsal_column_defs,
    _rehearsal_message,
    _rehearsal_results,
    _render_elements,
    _restore_migration,
    _reversal_provable,
    _step_elements,
    _stored_steps,
    _tag_applied,
    _tag_migration,
    _tracking_column_defs,
    _upgrade_column_def,
    _validation_problems,
    check_guards,
    checked_unique_ids,
    drift_lines,
    insert_sql,
    migration_checksum,
    plan_migration,
    quoted_columns,
    records_from_rows,
    records_select,
    rehearsal_failed,
    rehearsal_key,
    render_script,
    update_sql,
)
from sustained.types import RowValue, SqlValue

if TYPE_CHECKING:
    from sustained.autogenerate import IntrospectedTable
    from sustained.compilers.base import Compiler
    from sustained.guards import Guard, Verdict
    from sustained.introspect import Snapshot
    from sustained.model import Model
    from sustained.schema import TableOptions


class AsyncMigrator:
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
        checked_unique_ids(migrations)
        self._guards = list(guards or [])
        self._callbacks = callbacks or Callbacks()
        self._adapter = adapter
        self._migrations = list(migrations)
        self._table = table
        self._rehearsal_table = rehearsal_table
        self._dialect = dialect
        self._compiler = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False
        self._rehearsal_ready = False
        self._rehearsing = False

    @property
    def adapter(self) -> AsyncAdapter:
        """The adapter this migrator runs on."""
        return self._adapter

    @property
    def dialect(self) -> Dialects:
        """The dialect this migrator compiles for."""
        return self._dialect

    @property
    def compiler(self) -> "Compiler":
        """The compiler that renders this migrator's ddl steps."""
        return self._compiler

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    def _table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._table)

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

    @asynccontextmanager
    async def _migration_scope(self, transactional: bool = True) -> AsyncIterator[None]:
        """
        A transaction on engines whose schema changes roll back; a bare run
        followed by a commit on engines whose do not.

        `transactional` is the migration's own flag. A migration with
        transactional=False runs bare and commits at the end, so a
        statement the engine refuses inside a transaction block, such as
        CREATE INDEX CONCURRENTLY on Postgres, can run. Its tracking row is
        written after its statements, so a finished migration is still
        recorded. An adapter over a driver that opens its own transaction,
        such as DbApiAsyncAdapter over psycopg2, is the limit here: the
        driver still opens one, and such a statement still fails. Run it on
        an adapter that executes bare, such as AsyncpgAdapter.

        A rehearsal opens one transaction around the whole run and rolls it
        back at the end, so each migration runs bare and nothing commits.

        Nothing takes a failed non-transactional migration back. The
        statements that already ran stay in the database, and the tracking
        row says the attempt failed. The operator finishes or undoes the
        rest by hand and then runs repair(). On Postgres a failed CREATE
        INDEX CONCURRENTLY also leaves an invalid index, which needs a
        DROP INDEX of its own.
        """
        if self._rehearsing:
            yield
            return
        if transactional and self._compiler.supports_transactional_ddl():
            async with async_transaction(self._adapter, self._dialect):
                yield
            return
        yield
        await self._adapter.commit()

    async def _rollback_quietly(self) -> None:
        try:
            await self._adapter.rollback()
        except Exception:
            pass

    async def _execute(self, sql: str, params: Tuple[SqlValue, ...]) -> None:
        """Runs one parameterized statement, adapted for the dialect."""
        await self._adapter.execute(*self._compiler.prepare_execution(sql, params))

    async def _fetch(
        self, sql: str, params: Tuple[SqlValue, ...]
    ) -> Tuple[List[str], List[Sequence[RowValue]]]:
        """Runs one parameterized query, adapted for the dialect."""
        return await self._adapter.fetch(*self._compiler.prepare_execution(sql, params))

    @asynccontextmanager
    async def _lock_scope(self) -> AsyncIterator[None]:
        """
        Holds the engine's advisory lock, named after the tracking table,
        for the duration of a run, so concurrent migrators queue instead of
        racing. A no-op on engines without one.
        """
        lock_statements = self._compiler.migration_lock_sql(self._table)
        if not lock_statements:
            yield
            return
        for statement in lock_statements:
            await self._take_lock(statement)
        try:
            yield
        finally:
            for statement in self._compiler.migration_unlock_sql(self._table):
                try:
                    await self._adapter.execute(statement, ())
                except Exception:
                    pass

    async def _take_lock(self, statement: str) -> None:
        """
        Runs one lock statement and reads what it returned. MySQL and
        MSSQL report a refused lock in the result rather than raising, so
        a run that read nothing here could start while another migrator
        was working. A statement that returns no row reads as a lock that
        was not granted.
        """
        from sustained.exceptions import MigrationError

        _, rows = await self._adapter.fetch(statement, ())
        row: Optional[Sequence[RowValue]] = rows[0] if rows else None
        problem = self._compiler.migration_lock_problem(row)
        if problem is not None:
            raise MigrationError([_lock_message(self._table, problem)])

    async def _ensure_tracking_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._tracking_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._table_ddl_sql(),
            _tracking_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        await self._adapter.execute(sql, ())
        await self._adapter.commit()
        await self._upgrade_tracking_table()
        self._tracking_ready = True

    def _rehearsal_table_sql(self) -> str:
        return self._compiler.quote_identifier(self._rehearsal_table)

    def _rehearsal_table_ddl_sql(self) -> str:
        return self._compiler.quote_ddl_identifier(self._rehearsal_table)

    def _own_tables(self) -> Tuple[str, ...]:
        """
        The tables Sustained keeps for itself, which a rehearsal snapshot
        drops so its own bookkeeping never reads as an object left behind.
        """
        return (self._table, self._rehearsal_table)

    async def _ensure_rehearsal_table(self) -> None:
        from sustained.schema import build_create_table_sql

        if self._rehearsal_ready:
            return
        sql = build_create_table_sql(
            self._compiler,
            self._rehearsal_table_ddl_sql(),
            _rehearsal_column_defs(self._compiler.supports_constraints()),
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        await self._adapter.execute(sql, ())
        await self._adapter.commit()
        self._rehearsal_ready = True

    async def record_rehearsal(self, key: str, outcome: str = REHEARSAL_PASSED) -> None:
        """
        Writes the row for one rehearsal key, replacing any earlier row
        for the same key. Mirrors Migrator.record_rehearsal().
        """
        if outcome not in (REHEARSAL_PASSED, REHEARSAL_FAILED, REHEARSAL_OVERRIDE):
            raise ValueError(
                f"Unknown rehearsal outcome {outcome!r}; use "
                f"{REHEARSAL_PASSED!r}, {REHEARSAL_FAILED!r}, or "
                f"{REHEARSAL_OVERRIDE!r}."
            )
        await self._ensure_rehearsal_table()
        placeholder = self._compiler.placeholder()
        table = self._rehearsal_table_sql()
        await self._execute(
            f"DELETE FROM {table} WHERE rehearsal_key = {placeholder}", (key,)
        )
        values = ", ".join([placeholder] * 3)
        await self._execute(
            f"INSERT INTO {table} (rehearsal_key, outcome, rehearsed_at) "
            f"VALUES ({values})",
            (key, outcome, datetime.now(timezone.utc).isoformat()),
        )
        await self._adapter.commit()

    async def rehearsal_outcome(self, key: str) -> Optional[str]:
        """
        What the recorded rehearsal of this key proved: 'passed', 'failed',
        or None when no rehearsal has covered it.
        """
        await self._ensure_rehearsal_table()
        placeholder = self._compiler.placeholder()
        _, rows = await self._fetch(
            f"SELECT outcome FROM {self._rehearsal_table_sql()} "
            f"WHERE rehearsal_key = {placeholder}",
            (key,),
        )
        return None if not rows else str(rows[0][0])

    async def rehearsed(self, key: str) -> bool:
        """True when a passing rehearsal covers this key."""
        return await self.rehearsal_outcome(key) == REHEARSAL_PASSED

    async def _require_rehearsal_row(
        self,
        records: List[AppliedRecord],
        run: List[Migration],
        unrehearsed: bool,
        target: Optional[str] = None,
    ) -> None:
        """
        Stops a run that removes data unless a passing rehearsal covers
        exactly this content. Mirrors Migrator._require_rehearsal_row().
        """
        from sustained.exceptions import RehearsalRequired

        if unrehearsed:
            return
        destructive = _destructive_in(run, self._compiler)
        if not destructive:
            return
        outcome = await self.rehearsal_outcome(rehearsal_key(records, run))
        if outcome == REHEARSAL_PASSED:
            return
        raise RehearsalRequired(_rehearsal_message(destructive, outcome, target))

    async def _has_columns(self, columns: Tuple[str, ...]) -> bool:
        """Probes the tracking table for the given columns."""
        try:
            await self._adapter.fetch(
                f"SELECT {quoted_columns(self._compiler, *columns)} "
                f"FROM {self._table_sql()} WHERE 1 = 0",
                (),
            )
            return True
        except Exception:
            # A failed probe can poison an open transaction (Postgres
            # aborts it), so clear the slate before the next statement.
            await self._rollback_quietly()
            return False

    async def _upgrade_tracking_table(self) -> None:
        """
        Brings a tracking table written by an earlier version, which held
        only id and applied_at, up to the current shape. Missing columns
        are added nullable; seq and success are backfilled from the
        existing rows in applied order.
        """
        from sustained.schema import render_column_sql

        if await self._has_columns(_UPGRADE_COLUMNS):
            return
        added: List[str] = []
        for name in _UPGRADE_COLUMNS:
            if await self._has_columns((name,)):
                continue
            column_sql = render_column_sql(
                self._compiler, name, _upgrade_column_def(name), inline_pk=False
            )
            statement = self._compiler.compile_add_column(
                self._table_ddl_sql(), column_sql
            )
            await self._adapter.execute(statement, ())
            added.append(name)
        await self._adapter.commit()
        placeholder = self._compiler.placeholder()
        # Backfill only the columns this run added, and only where they are
        # still null, so values a partial earlier upgrade wrote survive.
        column = self._compiler.quote_identifier
        if "success" in added:
            await self._execute(
                f"UPDATE {self._table_sql()} SET {column('success')} = "
                f"{placeholder} WHERE {column('success')} IS NULL",
                (True,),
            )
        if "seq" in added:
            _, rows = await self._adapter.fetch(
                f"SELECT {column('id')} FROM {self._table_sql()} ORDER BY "
                f"{quoted_columns(self._compiler, 'applied_at', 'id')}",
                (),
            )
            for position, row in enumerate(rows, start=1):
                await self._execute(
                    f"UPDATE {self._table_sql()} SET {column('seq')} = "
                    f"{placeholder} WHERE {column('id')} = {placeholder} "
                    f"AND {column('seq')} IS NULL",
                    (position, row[0]),
                )
        await self._adapter.commit()

    async def _read_records(self) -> List[AppliedRecord]:
        """Reads the tracking table rows, assuming the table is there."""
        _, rows = await self._adapter.fetch(
            records_select(self._compiler, self._table_sql()),
            (),
        )
        return records_from_rows(rows)

    async def applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row in application order, creating
        the tracking table when it is missing.
        """
        await self._ensure_tracking_table()
        return await self._read_records()

    async def read_applied_records(self) -> List[AppliedRecord]:
        """
        Returns every tracking table row without writing anything.

        An empty list means the run has no history to read: either the
        tracking table does not exist yet, or it still has the shape an
        earlier version wrote and the read of its columns failed. The
        paths that only report on a run, such as script() and pending(),
        read the rows through this, since creating the table would change
        a database they say they leave alone.
        """
        if self._tracking_ready:
            return await self._read_records()
        try:
            return await self._read_records()
        except Exception:
            # A failed read can poison an open transaction, so clear the
            # slate before the next statement.
            await self._rollback_quietly()
            return []

    async def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in await self.applied_records() if r.success]

    async def read_applied(self) -> List[str]:
        """The applied migration ids, without creating the table."""
        return [r.id for r in await self.read_applied_records() if r.success]

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
        return render_script(
            self._compiler,
            self._table_sql(),
            self._migrations,
            await self.read_applied_records(),
            direction,
        )

    def _versioned(self) -> List[Migration]:
        return [m for m in self._migrations if not m.repeatable]

    def _repeatables(self) -> List[Migration]:
        return [m for m in self._migrations if m.repeatable]

    async def pending(self) -> List[Migration]:
        """
        Returns the registered migrations the next up() would run:
        versioned migrations without a successful row, then repeatables
        without one or whose checksum changed since the last run.
        """
        records = {r.id: r for r in await self.read_applied_records()}
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

    async def status(self) -> List[Tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(await self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    async def statuses(self) -> List[Tuple[str, str]]:
        """
        Returns (id, state) pairs for every registered migration. The
        state is 'applied', 'pending', or, for a repeatable whose
        contents changed since its last run, 'changed'.
        """
        records = {r.id: r for r in await self.applied_records()}
        return [
            (m.id, _migration_state(records.get(m.id), m)) for m in self._migrations
        ]

    def _insert_sql(self) -> str:
        return insert_sql(self._compiler, self._table_sql())

    def _update_sql(self) -> str:
        return update_sql(self._compiler, self._table_sql())

    async def validate(self, raise_on_problems: bool = True) -> List[str]:
        """
        Checks the tracking table against the registered migrations and
        returns the problems found: failed attempts, applied migrations
        this migrator does not know, checksum mismatches from edited
        migrations, and out-of-order pending migrations. Raises
        MigrationError when problems exist, unless raise_on_problems is
        False.
        """
        from sustained.exceptions import MigrationError

        problems = _validation_problems(self._migrations, await self.applied_records())
        if problems and raise_on_problems:
            raise MigrationError(problems)
        return problems

    async def repair(self) -> List[str]:
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
        records = await self.applied_records()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        actions: List[str] = []
        for record in records:
            if not record.success:
                await self._execute(
                    f"DELETE FROM {self._table_sql()} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder} "
                    f"AND {self._compiler.quote_identifier('success')} = "
                    f"{self._compiler.compile_boolean(False)}",
                    (record.id,),
                )
                actions.append(f"removed the failed attempt of '{record.id}'")
                continue
            migration = by_id.get(record.id)
            if migration is None or migration.repeatable:
                continue
            current = migration_checksum(migration)
            if current is not None and current != record.checksum:
                await self._execute(
                    f"UPDATE {self._table_sql()} SET "
                    f"{self._compiler.quote_identifier('checksum')} = "
                    f"{placeholder} WHERE "
                    f"{self._compiler.quote_identifier('id')} = {placeholder}",
                    (current, record.id),
                )
                actions.append(f"updated the stored checksum of '{record.id}'")
        await self._adapter.commit()
        return actions

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
        async with self._lock_scope():
            records = await self.applied_records()
            already_applied = {r.id for r in records if r.success}
            next_seq = _next_seq(records)
            recorded: List[str] = []
            for migration in versioned[: ids.index(target) + 1] + self._repeatables():
                if migration.id in already_applied:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                await self._execute(
                    self._insert_sql(),
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
            await self._adapter.commit()
            return recorded

    async def _record_failure(
        self,
        migration: Migration,
        seq: int,
        update: bool = False,
        generated: bool = False,
    ) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine whose schema changes do not roll back, where partial changes
        may remain. A repeatable that already has a row updates it in
        place. A failure to write the row never masks the original error. A
        rehearsal writes nothing: its whole run rolls back. A migration
        that asked for no transaction leaves partial changes on every
        engine, so it gets a row wherever it fails.
        """
        if self._rehearsing or (
            migration.transactional and self._compiler.supports_transactional_ddl()
        ):
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            checksum = migration_checksum(migration)
            steps = _stored_steps(migration, generated, self._compiler)
            if update:
                await self._execute(
                    self._update_sql(),
                    (
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        steps,
                        migration.id,
                    ),
                )
            else:
                await self._execute(
                    self._insert_sql(),
                    (
                        migration.id,
                        seq,
                        checksum,
                        timestamp,
                        None,
                        False,
                        generated,
                        steps,
                    ),
                )
            await self._adapter.commit()
        except Exception:
            pass

    async def up(
        self,
        target: Optional[str] = None,
        validate: bool = True,
        allow_out_of_order: bool = False,
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
        that second reading leaves the registered migrations applied, and
        lists their ids on the exception's `applied` attribute. The
        migrator's callbacks fire around the run, and each is awaited
        when it returns an awaitable.
        """
        callbacks = self._callbacks
        await self._fire(callbacks.before_migrate, self._adapter)
        try:
            applied = await self._run_up(
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
            await self._fire_on_error(error)
            raise
        if applied:
            await self._fire(callbacks.after_migrate, self._adapter, applied)
        return applied

    async def _fire(
        self, hook: Optional[Callable[..., CallbackResult]], *args: object
    ) -> None:
        """Calls one callback and awaits it when it returns an awaitable."""
        if hook is None:
            return
        result = hook(*args)
        if inspect.isawaitable(result):
            await result

    async def _fire_on_error(self, error: BaseException) -> None:
        """
        Hands a failed run to the on_error callback. A callback that
        raises is reported on stderr and set aside, so the run's own
        error reaches the caller.
        """
        hook = self._callbacks.on_error
        if hook is None:
            return
        try:
            await self._fire(
                hook, self._adapter, getattr(error, "migration_id", None), error
            )
        except Exception as callback_error:
            print(f"error: on_error raised {callback_error!r}", file=sys.stderr)

    async def _run_up(
        self,
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
    ) -> List[str]:
        """The run itself, without the callbacks up() wraps it in."""
        from sustained.exceptions import MigrationError

        if models is not None and target is not None:
            raise ValueError(
                "up() cannot take both models and a target: the generated "
                "migration always runs last, so a target would leave it out."
            )
        require_registered = models is None

        async with self._lock_scope():
            if models is not None:
                await self._ensure_tracking_table()

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

            records = await self.applied_records()
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
            versioned_now = [m for m in migrations if m.id not in already_applied]
            repeatables_now = [
                m
                for m in (self._repeatables() if target is None else [])
                if not _is_current(records_by_id.get(m.id), migration_checksum(m), True)
            ]
            # The registered set is checked before anything runs. The
            # order matches pending(), so a rehearsal of the same set
            # produces the same key.
            registered_run = versioned_now + repeatables_now
            warned: Set["Verdict"] = set()
            check_guards(self._guards, registered_run, self._dialect, warned)
            await self._require_rehearsal_row(
                records, registered_run, unrehearsed, target
            )
            final_run = list(registered_run)
            for migration in versioned_now:
                await self._apply(migration, next_seq, update=False)
                next_seq += 1
                applied_now.append(migration.id)
            if models is not None:
                generated = await self.plan(
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
                    try:
                        check_guards(self._guards, final_run, self._dialect, warned)
                        await self._require_rehearsal_row(
                            records, final_run, unrehearsed, target
                        )
                    except Exception as error:
                        _tag_applied(error, applied_now)
                        raise
                    # The migration joins the registered list only after
                    # it applied. A failed one left there would run again
                    # on the next up() of a long-lived migrator, and would
                    # run alongside a fresh diff of the same models.
                    await self._apply(generated, next_seq, update=False, generated=True)
                    self._migrations.append(generated)
                    next_seq += 1
                    applied_now.append(generated.id)
            for migration in repeatables_now:
                record = records_by_id.get(migration.id)
                await self._apply(migration, next_seq, update=record is not None)
                if record is None:
                    next_seq += 1
                applied_now.append(migration.id)
            if unrehearsed and _destructive_in(final_run, self._compiler):
                # The proof was waived, so the row says so. It never
                # unlocks a later run: only 'passed' does that.
                await self.record_rehearsal(
                    rehearsal_key(records, final_run), REHEARSAL_OVERRIDE
                )
            return applied_now

    async def _apply(
        self, migration: Migration, seq: int, update: bool, generated: bool = False
    ) -> None:
        """
        Runs one migration's up step and records it: an INSERT for a
        first run, an UPDATE in place when a repeatable re-runs, keeping
        its original seq. `generated` marks a migration the diff against
        the models produced, whose id nothing on disk carries. Its
        statements go on the tracking row, so down() can take it back.
        """
        try:
            async with self._migration_scope(migration.transactional):
                started = time.perf_counter()
                await self._run_step(migration.up)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                timestamp = datetime.now(timezone.utc).isoformat()
                checksum = migration_checksum(migration)
                steps = _stored_steps(migration, generated, self._compiler)
                if update:
                    await self._execute(
                        self._update_sql(),
                        (
                            checksum,
                            timestamp,
                            elapsed_ms,
                            True,
                            generated,
                            steps,
                            migration.id,
                        ),
                    )
                else:
                    await self._execute(
                        self._insert_sql(),
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
                    )
        except Exception as error:
            await self._record_failure(
                migration, seq, update=update, generated=generated
            )
            _tag_migration(error, migration.id)
            raise

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
        """
        from sustained.aio import in_async_transaction

        if not scratch:
            _check_rehearsable(self._dialect)
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
        # The lock sits outside the rehearsal transaction, so the rollback
        # runs before the lock is released. The state reads sit inside it,
        # so a concurrent migrator cannot apply between the read and the
        # rehearsal.
        async with self._lock_scope():
            await self.validate()
            pending = await self.pending()
            record_list = await self.applied_records()
            if not pending and models is None:
                return Rehearsal([], rehearsal_key(record_list, []))
            records = {r.id: r for r in record_list}
            seq = _next_seq(record_list)
            before = await self._snapshot()
            # What the row will cover: the pending set, plus the
            # generated migration once the diff produces one.
            attempted: List[Migration] = list(pending)
            has_drift = False
            self._rehearsing = True
            try:
                # Close whatever transaction the reads above opened, so the
                # explicit BEGIN starts a fresh one instead of warning.
                await self._rollback_quietly()
                begin = self._compiler.begin_transaction_sql()
                if begin is not None:
                    await self._adapter.execute(begin, ())
                ran: List[Migration] = []
                up_error: Optional[Tuple[str, str]] = None

                async def apply_each(group: List[Migration]) -> None:
                    nonlocal seq, up_error
                    for migration in group:
                        try:
                            await self._apply(
                                migration, seq, update=migration.id in records
                            )
                        except Exception as error:
                            up_error = (migration.id, str(error))
                            return
                        seq += 1
                        ran.append(migration)

                # The order matches up(): the versioned migrations, then
                # the generated one, then the repeatables, which may read
                # objects the generated migration creates.
                await apply_each([m for m in pending if not m.repeatable])
                landed: Dict[str, List[str]] = {}
                if models is not None and up_error is None:
                    # The diff is taken here, inside the rehearsal, so it
                    # sees the schema the pending migrations just left. The
                    # generated migration joins the run without being
                    # registered: nothing outside the rehearsal should see a
                    # migration the rollback is about to take back.
                    drift = await self.plan(
                        models,
                        allow_drops=allow_drops,
                        ignore_changed_columns=ignore_changed_columns,
                        migration_id=migration_id,
                        renames=renames,
                        table_renames=table_renames,
                        type_casts=type_casts,
                    )
                    if drift is not None:
                        attempted.append(drift)
                        has_drift = True
                        try:
                            await self._apply(drift, seq, update=False, generated=True)
                        except Exception as error:
                            up_error = (drift.id, str(error))
                        else:
                            seq += 1
                            ran.append(drift)
                            # The renames have already run, so the schema
                            # holds the new names. Passing the hints again
                            # would ask to rename objects that are gone.
                            landed[drift.id] = await self.drift(
                                models,
                                ignore_changed_columns=ignore_changed_columns,
                            )
                if up_error is None:
                    await apply_each([m for m in pending if m.repeatable])
                outcomes = {} if up_error else await self._rehearse_down(ran)
                reverted = None
                if before is not None and _reversal_provable(ran, outcomes):
                    from sustained.autogenerate import diff_snapshots

                    after = await self._snapshot()
                    if after is not None:
                        reverted = diff_snapshots(before, after)
                results = _rehearsal_results(ran, up_error, outcomes, landed, reverted)
            finally:
                self._rehearsing = False
                await self._roll_back_rehearsal()
            # The rehearsal row is written after the rollback, in its own
            # committed transaction, and still inside the lock.
            key = rehearsal_key(record_list, attempted)
            passed = not any(rehearsal_failed(r) for r in results)
            recorded = False
            if not scratch:
                await self.record_rehearsal(
                    key, REHEARSAL_PASSED if passed else REHEARSAL_FAILED
                )
                if passed and has_drift:
                    # A run without models applies the registered
                    # migrations and stops there, which this rehearsal
                    # also proved.
                    await self.record_rehearsal(rehearsal_key(record_list, pending))
                if passed:
                    for prefix_key in _destructive_prefix_keys(
                        record_list, pending, self._compiler
                    ):
                        await self.record_rehearsal(prefix_key)
                recorded = True
            return Rehearsal(results, key, recorded)

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
        """
        from sustained.introspect import Snapshot, _schema_plan

        read = SchemaRead()
        plan = _schema_plan(self._dialect, tuple(schemas))
        sql = next(plan)
        while True:
            try:
                _, rows = await self._adapter.fetch(sql, ())
            except Exception as error:
                read.record(sql, [], error)
                try:
                    sql = plan.throw(error)
                except StopIteration as stop:
                    return cast(Snapshot, stop.value), read
                continue
            read.record(sql, list(rows))
            try:
                sql = plan.send(list(rows))
            except StopIteration as stop:
                return cast(Snapshot, stop.value), read

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
        statement it runs.

        Objects the models do not declare are left alone, since a
        database may hold tables that hand-written migrations created.
        Pass allow_drops=True to generate the drops instead, or
        ignore_undeclared=False to refuse to generate while they exist.
        """
        from sustained.autogenerate import declared_schemas

        _, read = await self._read_schema(declared_schemas(models))
        return plan_migration(
            read.connection(),
            models,
            self._dialect,
            self._own_tables(),
            allow_drops=allow_drops,
            ignore_changed_columns=ignore_changed_columns,
            migration_id=migration_id,
            renames=renames,
            table_renames=table_renames,
            type_casts=type_casts,
            ignore_undeclared=ignore_undeclared,
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
        from sustained.autogenerate import declared_schemas

        snapshot, read = await self._read_schema(declared_schemas(models))
        return drift_lines(
            read.connection(),
            models,
            self._dialect,
            self._own_tables(),
            renames=renames,
            table_renames=table_renames,
            ignore_changed_columns=ignore_changed_columns,
            snapshot=snapshot,
        )

    async def _snapshot(self) -> Optional[Dict[str, "IntrospectedTable"]]:
        """
        The live schema, without the tracking table, or None when the
        database will not report it. Mirrors Migrator._snapshot().
        """
        try:
            schema, _ = await self._read_schema()
        except Exception:
            return None
        for name in self._own_tables():
            schema.pop(name.lower(), None)
        return dict(schema)

    async def _roll_back_rehearsal(self) -> None:
        """
        Takes back everything the rehearsal did. The statement runs first,
        because an adapter's own rollback() does nothing on drivers that
        run in autocommit until a transaction is opened, asyncpg among
        them; the adapter call follows to leave its bookkeeping straight.
        """
        statement = self._compiler.rollback_transaction_sql()
        if statement is not None:
            try:
                await self._adapter.execute(statement, ())
            except Exception:
                pass
        await self._rollback_quietly()

    async def _rehearse_down(
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
                    async with self._migration_scope(migration.transactional):
                        await self._run_step(cast(MigrationStep, migration.down))
                        await self._execute(
                            f"DELETE FROM {self._table_sql()} WHERE "
                            f"{self._compiler.quote_identifier('id')} = "
                            f"{placeholder}",
                            (migration.id,),
                        )
                except Exception as error:
                    outcomes[migration.id] = (False, str(error))
                    failed = migration.id
                else:
                    outcomes[migration.id] = (True, None)
        return outcomes

    async def _applied_versioned(self) -> List[str]:
        """Applied ids with the repeatables left out; down() skips them."""
        repeatable_ids = {m.id for m in self._repeatables()}
        return [i for i in await self.applied() if i not in repeatable_ids]

    async def _generated_migration(self, migration_id: str) -> Optional[Migration]:
        """
        The migration a generated tracking row describes, read back from
        the row itself. Mirrors Migrator._generated_migration(): the sync
        migrator writes these rows when it applies a diff against the
        models, and either migrator can take one back.
        """
        _, rows = await self._fetch(
            f"SELECT {self._compiler.quote_identifier('steps')} "
            f"FROM {self._table_sql()} WHERE "
            f"{self._compiler.quote_identifier('id')} = "
            f"{self._compiler.placeholder()}",
            (migration_id,),
        )
        if not rows or rows[0][0] is None:
            return None
        return _restore_migration(migration_id, str(rows[0][0]))

    async def down(self, steps: int = 1, allow_changed: bool = False) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step. Repeatables are never
        reverted. Returns the ids that were reverted.

        A migration generated from the models is reverted from its own
        tracking row, which holds the statements it ran. Every other
        migration must be registered with this migrator.

        `steps` counts migrations and must be 1 or more.

        A migration whose statements changed since it was applied raises
        MigrationError, because its down step describes the new contents
        and the database holds the old ones. Pass allow_changed=True to
        revert it with the down step as it stands now.
        """
        from sustained.exceptions import MigrationError

        _checked_steps(steps)
        async with self._lock_scope():
            await self._ensure_tracking_table()
            records = await self.applied_records()
            by_record = {r.id: r for r in records}
            repeatable_ids = {m.id for m in self._repeatables()}
            applied = [
                r.id for r in records if r.success and r.id not in repeatable_ids
            ]
            by_id = {m.id: m for m in self._migrations}
            placeholder = self._compiler.placeholder()
            reverted: List[str] = []
            for migration_id in reversed(applied[-steps:]):
                migration = by_id.get(migration_id)
                if migration is not None and not allow_changed:
                    if _changed_since_applied(migration, by_record.get(migration_id)):
                        raise MigrationError([_changed_down_message(migration_id)])
                if migration is None:
                    migration = await self._generated_migration(migration_id)
                if migration is None:
                    raise ValueError(
                        f"Applied migration '{migration_id}' is not registered "
                        "with this migrator; cannot revert."
                    )
                if migration.down is None:
                    raise ValueError(f"Migration '{migration_id}' has no down step.")
                async with self._migration_scope(migration.transactional):
                    await self._run_step(migration.down)
                    await self._execute(
                        f"DELETE FROM {self._table_sql()} WHERE "
                        f"{self._compiler.quote_identifier('id')} = "
                        f"{placeholder}",
                        (migration_id,),
                    )
                reverted.append(migration_id)
            return reverted

    async def down_to(self, target: str, allow_changed: bool = False) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        Repeatables are never reverted. `allow_changed` is passed to
        down().
        """
        applied = await self._applied_versioned()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        if not steps:
            return []
        return await self.down(steps, allow_changed=allow_changed)
