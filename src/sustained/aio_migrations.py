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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, List, Optional, Tuple

from sustained.aio import AsyncAdapter, async_transaction
from sustained.dialects import Dialects
from sustained.migrations import Migration, MigrationStep


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

    async def _ensure_tracking_table(self) -> None:
        from sustained.schema import String, Text, build_create_table_sql

        if self._compiler.supports_constraints():
            columns = {
                "id": String(255, primary_key=True),
                "applied_at": Text(nullable=False),
            }
        else:
            columns = {
                "id": String(255),
                "applied_at": Text(),
            }
        sql = build_create_table_sql(
            self._compiler,
            self._table_sql(),
            columns,
            if_not_exists=True,
            options=self._tracking_table_options,
        )
        await self._adapter.execute(sql, ())
        await self._adapter.commit()

    async def applied(self) -> List[str]:
        """Returns the applied migration ids in application order."""
        await self._ensure_tracking_table()
        _, rows = await self._adapter.fetch(
            f"SELECT id FROM {self._table_sql()} ORDER BY applied_at, id", ()
        )
        return [row[0] for row in rows]

    async def pending(self) -> List[Migration]:
        """Returns the registered migrations that have not been applied."""
        applied = set(await self.applied())
        return [m for m in self._migrations if m.id not in applied]

    async def status(self) -> List[Tuple[str, bool]]:
        """Returns (id, applied) pairs for every registered migration."""
        applied = set(await self.applied())
        return [(m.id, m.id in applied) for m in self._migrations]

    async def up(self, target: Optional[str] = None) -> List[str]:
        """
        Applies pending migrations in order, stopping after the target id
        when one is given. Returns the ids that were applied.
        """
        migrations = self._migrations
        if target is not None:
            ids = [m.id for m in migrations]
            if target not in ids:
                raise ValueError(f"Unknown migration target: {target!r}.")
            migrations = migrations[: ids.index(target) + 1]

        already_applied = set(await self.applied())
        placeholder = self._compiler.placeholder()
        applied_now: List[str] = []
        for migration in migrations:
            if migration.id in already_applied:
                continue
            async with self._migration_scope():
                await self._run_step(migration.up)
                timestamp = datetime.now(timezone.utc).isoformat()
                await self._adapter.execute(
                    f"INSERT INTO {self._table_sql()} (id, applied_at) "
                    f"VALUES ({placeholder}, {placeholder})",
                    (migration.id, timestamp),
                )
            applied_now.append(migration.id)
        return applied_now

    async def down(self, steps: int = 1) -> List[str]:
        """
        Reverts the most recently applied migrations, newest first. Every
        reverted migration must define a down step and be registered with
        this migrator. Returns the ids that were reverted.
        """
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
