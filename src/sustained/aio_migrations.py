"""
Async migration runner.

AsyncMigrator mirrors Migrator on an AsyncAdapter: same Migration objects,
same tracking table, same ordering rules. String and list steps execute
through the adapter; callable steps receive the adapter and are awaited
when they return a coroutine. Each migration runs inside an
async_transaction() block.
"""

from __future__ import annotations

import inspect
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Optional, Tuple

from sustained.aio import AsyncAdapter, async_transaction
from sustained.dialects import Dialects
from sustained.migrations import (
    _RECORDS_SELECT,
    _UPGRADE_COLUMNS,
    AppliedRecord,
    Migration,
    MigrationStep,
    _next_seq,
    _tracking_column_defs,
    _upgrade_column_def,
    _validation_problems,
    migration_checksum,
)


class AsyncMigrator:
    """Applies and reverts an ordered list of migrations on an adapter."""

    def __init__(
        self,
        adapter: AsyncAdapter,
        migrations: List[Migration],
        table: str = "sustained_migrations",
        dialect: Dialects = Dialects.DEFAULT,
        tracking_table_options: Any = None,
    ) -> None:
        ids = [m.id for m in migrations]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate migration ids: {sorted(duplicates)}.")
        self._adapter = adapter
        self._migrations = list(migrations)
        self._table = table
        self._dialect = dialect
        self._compiler = Dialects.get_compiler(dialect)
        self._tracking_table_options = tracking_table_options
        self._tracking_ready = False

    def _table_sql(self) -> str:
        return self._compiler.quote_identifier(self._table)

    async def _run_step(self, step: MigrationStep) -> None:
        if callable(step):
            result = step(self._adapter)
            if inspect.isawaitable(result):
                await result
            return
        statements = [step] if isinstance(step, str) else list(step)
        for statement in statements:
            await self._adapter.execute(statement, ())

    @asynccontextmanager
    async def _migration_scope(self) -> AsyncIterator[None]:
        """
        A transaction on engines that have them; a bare run followed by a
        commit on engines that do not.
        """
        if self._compiler.supports_transactions():
            async with async_transaction(self._adapter):
                yield
            return
        yield
        await self._adapter.commit()

    async def _rollback_quietly(self) -> None:
        try:
            await self._adapter.rollback()
        except Exception:
            pass

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
            await self._adapter.execute(statement, ())
        try:
            yield
        finally:
            for statement in self._compiler.migration_unlock_sql(self._table):
                try:
                    await self._adapter.execute(statement, ())
                except Exception:
                    pass

    async def _ensure_tracking_table(self) -> None:
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
        await self._adapter.execute(sql, ())
        await self._adapter.commit()
        await self._upgrade_tracking_table()
        self._tracking_ready = True

    async def _has_columns(self, columns: Tuple[str, ...]) -> bool:
        """Probes the tracking table for the given columns."""
        try:
            await self._adapter.fetch(
                f"SELECT {', '.join(columns)} FROM {self._table_sql()} WHERE 1 = 0",
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
            statement = self._compiler.compile_add_column(self._table_sql(), column_sql)
            await self._adapter.execute(statement, ())
            added.append(name)
        await self._adapter.commit()
        if "seq" not in added and "success" not in added:
            return
        _, rows = await self._adapter.fetch(
            f"SELECT id FROM {self._table_sql()} ORDER BY applied_at, id", ()
        )
        placeholder = self._compiler.placeholder()
        for position, row in enumerate(rows, start=1):
            await self._adapter.execute(
                f"UPDATE {self._table_sql()} SET seq = {placeholder}, "
                f"success = {placeholder} WHERE id = {placeholder}",
                (position, True, row[0]),
            )
        await self._adapter.commit()

    async def applied_records(self) -> List[AppliedRecord]:
        """Returns every tracking table row in application order."""
        await self._ensure_tracking_table()
        _, rows = await self._adapter.fetch(
            f"{_RECORDS_SELECT} {self._table_sql()} ORDER BY seq, applied_at, id",
            (),
        )
        return [AppliedRecord(row[0], row[1], row[2], bool(row[3])) for row in rows]

    async def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        return [r.id for r in await self.applied_records() if r.success]

    async def pending(self) -> List[Migration]:
        """Returns the registered migrations that have not been applied."""
        applied = set(await self.applied())
        return [m for m in self._migrations if m.id not in applied]

    async def status(self) -> List[Tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(await self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    def _insert_sql(self) -> str:
        placeholder = self._compiler.placeholder()
        values = ", ".join([placeholder] * 6)
        return (
            f"INSERT INTO {self._table_sql()} "
            f"(id, seq, checksum, applied_at, execution_ms, success) "
            f"VALUES ({values})"
        )

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
        """
        records = await self.applied_records()
        by_id = {m.id: m for m in self._migrations}
        placeholder = self._compiler.placeholder()
        actions: List[str] = []
        for record in records:
            if not record.success:
                await self._adapter.execute(
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
                await self._adapter.execute(
                    f"UPDATE {self._table_sql()} SET checksum = {placeholder} "
                    f"WHERE id = {placeholder}",
                    (current, record.id),
                )
                actions.append(f"updated the stored checksum of '{record.id}'")
        await self._adapter.commit()
        return actions

    async def _record_failure(self, migration: Migration, seq: int) -> None:
        """
        Writes a failed-attempt row after a migration step raised on an
        engine without transactions, where partial changes may remain. A
        failure to write the row never masks the original error.
        """
        if self._compiler.supports_transactions():
            return
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            await self._adapter.execute(
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
            await self._adapter.commit()
        except Exception:
            pass

    async def up(
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

        async with self._lock_scope():
            records = await self.applied_records()
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
                    async with self._migration_scope():
                        started = time.perf_counter()
                        await self._run_step(migration.up)
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        timestamp = datetime.now(timezone.utc).isoformat()
                        await self._adapter.execute(
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
                    await self._record_failure(migration, next_seq)
                    raise
                next_seq += 1
                applied_now.append(migration.id)
            return applied_now

    async def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step and be registered with
        this migrator. Returns the ids that were reverted.
        """
        async with self._lock_scope():
            applied = await self.applied()
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
                async with self._migration_scope():
                    await self._run_step(migration.down)
                    await self._adapter.execute(
                        f"DELETE FROM {self._table_sql()} WHERE id = {placeholder}",
                        (migration_id,),
                    )
                reverted.append(migration_id)
            return reverted

    async def down_to(self, target: str) -> List[str]:
        """
        Reverts applied migrations newest-first until the target is the
        most recent applied migration. The target itself stays applied.
        """
        applied = await self.applied()
        if target not in applied:
            raise ValueError(f"Migration '{target}' is not applied.")
        steps = len(applied) - applied.index(target) - 1
        return await self.down(steps) if steps else []
